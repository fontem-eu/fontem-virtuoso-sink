"""Declare what no package database in this image lists, for its SBOM.

- isql and the Virtuoso ODBC libraries, copied from the Virtuoso image:
  identified by that image's own declaration.
- CPython's extension modules and stable-ABI library: the python:slim base
  builds Python from source into /usr/local, so dpkg does not know them.
  syft identifies the interpreter itself (pkg:generic/python@<version>);
  this adds the files around it to that same component.

docker-build-sign adds the result to the SBOM and requires every executable
file to be covered. Usage: sbom-declare.py <virtuoso declared.json>
"""
import json
import sys
import sysconfig


def main(virtuoso_declared):
    virtuoso = next(c for c in json.load(open(virtuoso_declared))["components"]
                    if c["name"] == "virtuoso-opensource")
    full = sys.version.split()[0]                      # 3.14.8
    stdlib = sysconfig.get_paths()["stdlib"]          # /usr/local/lib/python3.14
    comps = [
        dict(virtuoso, paths=["/opt/virtuoso-opensource/bin/isql", "/opt/virtuoso-opensource/lib/"]),
        {"name": "python", "version": full, "purl": f"pkg:generic/python@{full}",
         "cpe": f"cpe:2.3:a:python_software_foundation:python:{full}:*:*:*:*:*:*:*",
         "paths": [f"{stdlib}/lib-dynload/", f"{sysconfig.get_config_var('LIBPL')}/",
                   f"{sysconfig.get_config_var('LIBDIR')}/libpython3.so"]},
    ]
    json.dump({"components": comps}, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main(sys.argv[1])
