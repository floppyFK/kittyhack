"""Backend package — door control loop, model runtime, MQTT bridge.

Public API matches the former ``src.backend`` module so existing imports keep working:
``from src.backend import backend_main, manual_door_override, ...``.

Heavy symbols are resolved lazily so lightweight modules (e.g. ``entry_policy``)
can be imported in unit tests without pulling the full loop / camera stack.
"""

__all__ = [
    "backend_main",
    "manual_door_override",
    "model_handler",
    "reload_model_handler_runtime",
    "update_mqtt_config",
    "update_mqtt_language",
    "restart_mqtt",
    "motion_state",
    "motion_state_lock",
]


def __getattr__(name: str):
    """Lazy-resolve public backend symbols."""
    if name in ("backend_main", "motion_state", "motion_state_lock"):
        from src.backend import loop as _loop

        return getattr(_loop, name)
    if name == "reload_model_handler_runtime":
        from src.backend import model_runtime

        return model_runtime.reload_model_handler_runtime
    if name in (
        "manual_door_override",
        "update_mqtt_config",
        "update_mqtt_language",
        "restart_mqtt",
    ):
        from src.backend import mqtt_bridge

        return getattr(mqtt_bridge, name)
    if name == "model_handler":
        from src.backend import model_runtime

        return model_runtime.model_handler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
