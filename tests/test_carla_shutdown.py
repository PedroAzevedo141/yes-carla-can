"""CARLA subscriptions must not outlive the Python client or vehicle restart."""
from types import SimpleNamespace
from unittest.mock import Mock

import gui.world as world_module


def test_shutdown_removes_tick_callback_before_destroying_sensors():
    calls = []
    world = world_module.World.__new__(world_module.World)
    world._tick_callback_id = 0  # Zero is also a valid registration identifier.
    world.world = SimpleNamespace(remove_on_tick=lambda identifier: calls.append(("unsubscribe", identifier)))
    world.radar_sensor = None
    world.rgb_camera_sensor = None
    world.camera_manager = None
    world.player = SimpleNamespace(destroy=lambda: calls.append("destroy-player"))
    for name in ["collision_sensor", "lane_invasion_sensor", "gnss_sensor", "imu_sensor"]:
        actor = SimpleNamespace(stop=lambda: calls.append("stop-sensor"), destroy=lambda: calls.append("destroy-sensor"))
        setattr(world, name, SimpleNamespace(sensor=actor))
    world.destroy()
    assert calls[0] == ("unsubscribe", 0)
    assert calls[-1] == "destroy-player"
    assert world._tick_callback_id is None


def test_restarting_vehicle_replaces_tick_subscription(monkeypatch):
    calls, active = [], set()
    next_identifier = iter([10, 11])
    def subscribe(callback):
        identifier = next(next_identifier)
        active.add(identifier)
        calls.append(("subscribe", identifier))
        return identifier
    def unsubscribe(identifier):
        active.remove(identifier)
        calls.append(("unsubscribe", identifier))
    def player():
        transform = SimpleNamespace(location=SimpleNamespace(z=0), rotation=SimpleNamespace(roll=0, pitch=0))
        return SimpleNamespace(get_transform=lambda: transform, destroy=lambda: calls.append("destroy-player"))
    engine = SimpleNamespace(on_tick=subscribe, remove_on_tick=unsubscribe,
                             wait_for_tick=lambda: None, try_spawn_actor=lambda *_: player())
    blueprint = SimpleNamespace(has_attribute=lambda _: False, set_attribute=lambda *_: None)
    monkeypatch.setattr(world_module, "get_actor_blueprints", lambda *_: [blueprint])
    monkeypatch.setattr(world_module, "get_actor_display_name", lambda _: "test vehicle")
    for name in ["CollisionSensor", "LaneInvasionSensor", "GnssSensor", "IMUSensor"]:
        monkeypatch.setattr(world_module, name, lambda *_: SimpleNamespace(sensor=Mock()))
    class Camera:
        index = 0
        transform_index = 0
        def __init__(self, *_):
            self.sensor = Mock()
        def set_sensor(self, index, **_):
            self.index = index
    monkeypatch.setattr(world_module, "CameraManager", Camera)
    monkeypatch.setattr(world_module, "RGBCameraSensor", lambda *_: Mock())
    world = world_module.World.__new__(world_module.World)
    world.world, world.map = engine, SimpleNamespace(get_spawn_points=lambda: ["spawn"])
    world._tick_callback_id = None
    world.player = world.camera_manager = world.rgb_camera_sensor = world.radar_sensor = None
    world._actor_filter, world._actor_generation, world._gamma = "vehicle.*", "2", 2.2
    world.actor_role_name, world.sync = "hero", False
    world.can_net, world.hud = Mock(), Mock()
    world.modify_vehicle_physics = lambda _: None
    world.restart()
    assert active == {10}
    world.restart()
    assert active == {11}
    assert calls.index(("unsubscribe", 10)) < calls.index("destroy-player") < calls.index(("subscribe", 11))
    world.destroy()
    assert active == set()
