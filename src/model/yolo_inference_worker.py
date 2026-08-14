"""YOLO inference subprocess (CPU-affinity-limited worker).

Import-order is load-bearing. Python 3.14's default multiprocessing start
method on Linux is ``forkserver``, which starts a *fresh* interpreter and
re-imports this module before the worker function runs. OpenMP/NCNN/PyTorch
read ``OMP_NUM_THREADS`` and the CPU affinity mask at first import. If cv2,
numpy, torch, or ultralytics are imported first, they spawn one thread per
core; pinning to a single core afterwards oversubscribes that core and cuts
throughput by roughly the core count (observed: ~2.95 fps with fork on
Python 3.12 vs ~0.72 fps with forkserver on Python 3.14 on the Kittyflap).

This module must not import those libraries at top level. ``src.model``
``__init__`` is lazy for the same reason.
"""
from __future__ import annotations

import logging
import os

_THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OMP_THREAD_LIMIT",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "TORCH_NUM_THREADS",
    "NNPACK_NUM_THREADS",
)


def _normalized_thread_count(num_threads) -> int:
    """Return a positive int thread/core limit."""
    try:
        n = int(num_threads) if num_threads else 1
    except (TypeError, ValueError):
        n = 1
    return max(1, n)


def configure_worker_compute(num_threads=1) -> list[int]:
    """Cap native thread pools and pin CPU affinity *before* OpenMP inits.

    Returns the affinity list that was requested (and applied when possible).
    Safe to call more than once; later OpenMP init will see the env vars.
    """
    n = _normalized_thread_count(num_threads)
    n_str = str(n)
    for key in _THREAD_ENV_VARS:
        os.environ[key] = n_str
    # Passive wait avoids the default ~200 ms OpenMP spinlock that otherwise
    # burns the only allowed core after each parallel region.
    os.environ["OMP_WAIT_POLICY"] = "PASSIVE"
    os.environ["KMP_BLOCKTIME"] = "0"
    os.environ["OMP_DYNAMIC"] = "FALSE"

    requested = list(range(n))
    cores_to_use = list(requested)
    try:
        if hasattr(os, "sched_getaffinity"):
            allowed = sorted(os.sched_getaffinity(0))
            if allowed:
                cores_to_use = [c for c in requested if c in allowed]
                if not cores_to_use:
                    cores_to_use = list(allowed[:n])
        if cores_to_use and hasattr(os, "sched_setaffinity"):
            os.sched_setaffinity(0, cores_to_use)
    except Exception as e:
        logging.warning(f"[MODEL] Failed to set CPU affinity; continuing without pinning: {e}")
    return cores_to_use


def apply_runtime_thread_limits(num_threads=1, model=None) -> None:
    """Apply thread caps on already-imported cv2/torch/ncnn (and the YOLO net)."""
    n = _normalized_thread_count(num_threads)
    try:
        import cv2

        cv2.setNumThreads(n)
        try:
            cv2.ocl.setUseOpenCL(False)
        except Exception:
            pass
    except Exception:
        pass

    try:
        import torch

        torch.set_num_threads(n)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass
    except Exception:
        pass

    try:
        import ncnn

        if hasattr(ncnn, "set_omp_num_threads"):
            ncnn.set_omp_num_threads(n)
    except Exception:
        pass

    backend = getattr(model, "model", None) if model is not None else None
    net = getattr(backend, "net", None)
    if net is not None and hasattr(net, "opt"):
        try:
            net.opt.num_threads = n
        except Exception:
            pass


def yolo_model_worker_process(
    model_path,
    input_queue,
    output_queue,
    num_threads=1,
    inference_device="cpu",
):
    """YOLO inference subprocess. Must stay picklable (module-level target)."""
    n = _normalized_thread_count(num_threads)
    cores_to_use = configure_worker_compute(n)

    try:
        from src.baseconfig import CONFIG, configure_logging
        from src.model.detection import _parse_yolo_detection_results
        from ultralytics import YOLO

        apply_runtime_thread_limits(n)

        logging.getLogger("ultralytics").setLevel(logging.WARNING)
        logging.getLogger("ultralytics.yolo.engine.model").setLevel(logging.WARNING)
        if cores_to_use:
            logging.info(
                f"[MODEL] Worker process running on CPU cores {cores_to_use} "
                f"with {n} native thread(s)"
            )
        else:
            logging.info(f"[MODEL] Worker process running without CPU affinity ({n} native thread(s))")

        model = YOLO(model_path, task="detect", verbose=False)
        apply_runtime_thread_limits(n, model=model)
        configure_logging(CONFIG["LOGLEVEL"])

        while True:
            job = input_queue.get()
            if job is None:
                break

            job_id, frame, input_size, labels, cat_names, min_threshold = job

            _worker_kwargs: dict = dict(stream=True, imgsz=input_size, verbose=False)
            if inference_device != "cpu":
                _worker_kwargs["device"] = inference_device
            results = model(frame, **_worker_kwargs)

            mouse_probability, own_cat_probability, detected_objects = _parse_yolo_detection_results(
                results,
                labels,
                cat_names,
                min_threshold,
            )

            output_queue.put((job_id, mouse_probability, own_cat_probability, detected_objects))

    except Exception as e:
        logging.error(f"[MODEL] Worker process error: {e}")
        import traceback
        logging.error(traceback.format_exc())
    finally:
        logging.info("[MODEL] Worker process exiting")
