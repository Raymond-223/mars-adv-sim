# ROS 2 repair fixture

Minimal `ament_python` ROS 2 package used by the MOSAIC-Ω scenario-A integration test.
The controller intentionally contains one deterministic defect. The repair pipeline
must discover the package, diagnose the failing pytest, patch it, build, retest and
produce evidence. On a ROS 2 machine the build action uses `colcon build`; CI without
ROS uses an explicitly recorded compile-only fallback and never reports it as colcon.
