"""YOLO inference subprocess (CPU-affinity-limited worker).

Import-order is load-bearing. Python 3.14's default multiprocessing start
method on Linux is ``forkserver``, which starts a *fresh* interpreter and
re-imports this module before the worker function runs.

The worker talks to NCNN directly (no Ultralytics/PyTorch). Ultralytics still
letterboxes and runs NMS through torch even for NCNN models; torch 2.9 on
the Kittyflap is far slower than the 3.12 / torch 2.7 stack (~0.72 vs ~2.95 fps).

This module must not import cv2/numpy/torch/ultralytics/ncnn at top level.
``src.model`` ``__init__`` is lazy for the same reason.
"""
from __future__ import annotations

import logging
import os
import time as tm

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


def apply_runtime_thread_limits(num_threads=1) -> None:
    """Apply thread caps on already-imported cv2/ncnn."""
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
        import ncnn

        if hasattr(ncnn, "set_omp_num_threads"):
            ncnn.set_omp_num_threads(n)
    except Exception:
        pass


def yolo_model_worker_process(
    model_path,
    input_queue,
    output_queue,
    num_threads=1,
    inference_device="cpu",
):
    """NCNN inference subprocess. Must stay picklable (module-level target)."""
    n = _normalized_thread_count(num_threads)
    cores_to_use = configure_worker_compute(n)

    try:
        from src.baseconfig import CONFIG, configure_logging
        from src.model.ncnn_detect import NcnnYoloDetector

        apply_runtime_thread_limits(n)
        configure_logging(CONFIG["LOGLEVEL"])

        if cores_to_use:
            logging.info(
                f"[MODEL] Worker process running on CPU cores {cores_to_use} "
                f"with {n} native thread(s) (NCNN, no torch)"
            )
        else:
            logging.info(
                f"[MODEL] Worker process running without CPU affinity "
                f"({n} native thread(s), NCNN, no torch)"
            )

        detector = NcnnYoloDetector(model_path, num_threads=n)
        first = True

        while True:
            job = input_queue.get()
            if job is None:
                break

            job_id, frame, input_size, labels, cat_names, min_threshold = job
            t0 = tm.perf_counter()
            mouse_probability, own_cat_probability, detected_objects = detector.predict(
                frame,
                labels,
                cat_names,
                min_threshold,
                input_size=input_size,
            )
            if first:
                logging.info(
                    f"[MODEL] First NCNN inference took {tm.perf_counter() - t0:.3f}s "
                    f"(imgsz={detector.imgsz})"
                )
                first = False

            output_queue.put((job_id, mouse_probability, own_cat_probability, detected_objects))

    except Exception as e:
        logging.error(f"[MODEL] Worker process error: {e}")
        import traceback
        logging.error(traceback.format_exc())
    finally:
        logging.info("[MODEL] Worker process exiting")
