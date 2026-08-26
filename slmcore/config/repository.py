from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .store import SLMConfigStore


class SLMConfigRepository:
    """Directory-bound persistence facade for complete SLM configs."""

    def __init__(self,directory: Any,registries,store: SLMConfigStore | None=None):
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(parents=True,exist_ok=True)
        self.registries = registries
        self.store = store or SLMConfigStore()

    def resolve(self,path_or_name: Any) -> Path:
        path = Path(path_or_name).expanduser()
        if not path.is_absolute():
            path = self.directory / path
        return path

    def destination(self,name: str) -> Path:
        filename = os.path.basename(str(name or "").strip())
        if not filename:
            raise ValueError("Config name cannot be empty")
        if Path(filename).suffix.lower() not in (".h5",".hdf5"):
            filename += ".h5"
        return self.directory / filename

    def list(self):
        return self.store.list_configs(self.directory)

    def load(self,path_or_name):
        return self.store.load(self.resolve(path_or_name),self.registries)

    def read_metadata(self,path_or_name):
        return self.store.read_metadata(self.resolve(path_or_name))

    def read_compiled_frame(self,path_or_name):
        return self.store.read_compiled_frame(self.resolve(path_or_name))

    def inspect(self,path_or_name):
        return self.store.inspect(self.resolve(path_or_name),self.registries)

    def compare(self,path_or_name,config):
        return self.store.compare(
            self.resolve(path_or_name),config,self.registries,
        )

    def save(self,name_or_path,config,info="",*,overwrite=False):
        path = self.resolve(name_or_path)
        if path.parent == self.directory and not path.suffix:
            path = self.destination(path.name)
        return self.store.save(path,config,info,overwrite=overwrite)

    def rename(self,source,new_name,*,overwrite=False):
        return self.store.rename(
            self.resolve(source),self.destination(new_name),overwrite=overwrite,
        )

    def duplicate(self,source,new_name,*,overwrite=False):
        return self.store.duplicate(
            self.resolve(source),self.destination(new_name),overwrite=overwrite,
        )

    def delete(self,path_or_name) -> None:
        self.store.delete(self.resolve(path_or_name))
