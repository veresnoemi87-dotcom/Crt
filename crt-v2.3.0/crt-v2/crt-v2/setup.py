from setuptools import setup

setup(
    name="crt-cli",
    version="2.3.0",
    py_modules=["crt"],
    install_requires=[],
    extras_require={
        "graphics": ["pygame>=2.0"],
    },
    entry_points={
        "console_scripts": [
            "crt=crt:main",
        ],
    },
    python_requires=">=3.8",
)
