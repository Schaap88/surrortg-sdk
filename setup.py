import setuptools

# with open("README.md", "r") as fh:
#    long_description = fh.read()

setuptools.setup(
    name="surrortg",
    version="0.0.4",
    install_requires=[
        "aiohttp==3.11.18",
        "pigpio",
        "python-socketio==5.16.4",
        "pyyaml",
        "toml",
    ],
    # author="",
    # author_email="",
    description="SurroRTG SDK",
    # long_description=long_description,
    # long_description_content_type="text/markdown",
    # url="",
    packages=setuptools.find_packages(include="surrortg"),
    # classifiers=[],
    python_requires=">=3.8",
)
