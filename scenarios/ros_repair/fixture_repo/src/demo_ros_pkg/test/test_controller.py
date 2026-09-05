from demo_ros_pkg.controller import combine_wheel_commands


def test_combine_wheel_commands():
    assert combine_wheel_commands(3, 4) == 7
