from setuptools import setup, find_packages
from pathlib import Path

long_description = ""
readme = Path(__file__).parent / "README.md"
if readme.exists():
    long_description = readme.read_text(encoding="utf-8")

setup(
    name="swarmesh",
    version="0.1.0",
    author="SwarMesh",
    author_email="hello@swarmesh.xyz",
    description="Connect your AI agent to the SwarMesh network",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/A-tndn/swarmesh",
    project_urls={
        "Homepage": "https://swarmesh.xyz",
        "Source": "https://github.com/A-tndn/swarmesh",
        "Issues": "https://github.com/A-tndn/swarmesh/issues",
    },
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=["requests>=2.20.0"],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
    keywords="swarmesh agent mesh ai decentralized",
    license="MIT",
)
