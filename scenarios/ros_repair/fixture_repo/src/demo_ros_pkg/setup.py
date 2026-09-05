from setuptools import find_packages, setup

package_name = "demo_ros_pkg"
setup(
    name=package_name,
    version="0.0.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="MOSAIC Team",
    maintainer_email="mosaic@example.invalid",
    description="MOSAIC-Ω deterministic ROS repair fixture",
    license="Apache-2.0",
    tests_require=["pytest"],
)
