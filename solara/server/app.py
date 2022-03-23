import atexit
import contextlib
import dataclasses
import importlib.util
import logging
import os
import pickle
import threading
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import ipywidgets as widgets
import react_ipywidgets as react
from starlette.websockets import WebSocket

from . import kernel

COOKIE_KEY_CONTEXT_ID = "solara-context-id"


logger = logging.getLogger("solara.server.app")
state_directory = Path(".") / "states"
state_directory.mkdir(exist_ok=True)


@contextlib.contextmanager
def cwd(path):
    cwd = os.getcwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(cwd)


@dataclasses.dataclass
class AppContext:
    id: str
    kernel: kernel.Kernel
    control_sockets: List[WebSocket]
    # this is the 'private' version of the normally global ipywidgets.Widgets.widget dict
    # see patch.py
    widgets: Dict[str, widgets.Widget]
    # same, for ipyvue templates
    # see patch.py
    templates: Dict[str, widgets.Widget]
    # anything we need to attach to the context
    # e.g. for a react app the render context, so that we can store/restore the state
    app_object: Optional[Any] = None

    def display(self, *args):
        print(args)

    def __enter__(self):
        key = get_current_thread_key()
        current_context[key] = self

    def __exit__(self, *args):
        key = get_current_thread_key()
        current_context[key] = None

    def close(self):
        with self:
            widgets.Widget.close_all()
            # what if we reference eachother
            # import gc
            # gc.collect()

    def state_reset(self):
        path = state_directory / f"{self.id}.pickle"
        path = path.absolute()
        path.unlink(missing_ok=True)
        del contexts[self.id]
        key = get_current_thread_key()
        assert current_context[key] is self
        del current_context[key]

    def state_save(self, state_directory: os.PathLike):
        path = state_directory / f"{self.id}.pickle"
        render_context = self.app_object
        if render_context is not None:
            render_context = cast(react.core._RenderContext, render_context)
            state = render_context.state_get()
            with path.open("wb") as f:
                pickle.dump(state, f)


contexts: Dict[str, AppContext] = {}
# maps from thread key to AppContext, if AppContext is None, it exists, but is not set as current
current_context: Dict[str, Optional[AppContext]] = {}


def get_current_thread_key() -> str:
    thread = threading.currentThread()
    return get_thread_key(thread)


def get_thread_key(thread: threading.Thread) -> str:
    thread_key = thread._name
    return thread_key


def set_context_for_thread(context: AppContext, thread: threading.Thread):
    key = get_thread_key(thread)
    contexts[key] = context
    current_context[key] = context


def get_current_context() -> AppContext:
    thread_key = get_current_thread_key()
    if thread_key not in current_context:
        raise RuntimeError(
            f"Tried to get the current context for thread {thread_key}, but no known context found. This might be a bug in Solara. (known contexts: {list(current_context.keys())}"
        )
    context = current_context.get(thread_key)
    if context is None:
        raise RuntimeError(
            f"Tried to get the current context for thread {thread_key}, although the context is know, it was not set for this thread. This might be a bug in Solara."
        )
    return context


class AppType(str, Enum):
    SCRIPT = "script"
    NOTEBOOK = "notebook"
    MODULE = "module"


class AppScript:
    def __init__(self, name, default_app_name="app"):
        self.fullname = name
        self.app_name = default_app_name
        if ":" in self.fullname:
            self.name, self.app_name = self.fullname.split(":")
        else:
            self.name = name
        self.path: Path = Path(self.name)
        if self.name.endswith(".py"):
            self.type = AppType.SCRIPT
        elif self.name.endswith(".ipynb"):
            self.type = AppType.NOTEBOOK
        else:
            self.type = AppType.MODULE
            spec = importlib.util.find_spec(self.name)
            assert spec is not None
            assert spec.origin is not None
            self.path = Path(spec.origin)

    def run(self):
        context = get_current_context()
        # local_scope = {"display": display_solara, "__name__": "__main__", "__file__": filename, "__package__": "solara.examples"}
        local_scope = {"display": context.display, "__name__": "__main__", "__file__": self.path}
        ignore = list(local_scope)
        if self.type == AppType.SCRIPT:
            with open(self.path) as f:
                ast = compile(f.read(), self.path, "exec")
                exec(ast, local_scope)
        elif self.type == AppType.NOTEBOOK:
            import nbformat

            nb: nbformat.NotebookNode = nbformat.read(self.path, 4)
            with cwd(Path(self.path).parent):
                for cell_index, cell in enumerate(nb.cells):
                    cell_index += 1  # used 1 based
                    if cell.cell_type == "code":
                        source = cell.source
                        cell_path = f"{self.path} input cell {cell_index}"
                        ast = compile(source, cell_path, "exec")
                        exec(ast, local_scope)
        elif self.type == AppType.MODULE:
            mod = importlib.import_module(self.name)
            local_scope = mod.__dict__
        else:
            raise ValueError(self.type)

        app = local_scope.get(self.app_name)
        if app is None:
            import difflib

            options = [k for k in list(local_scope) if k not in ignore and not k.startswith("_")]
            matches = difflib.get_close_matches(self.app_name, options)
            msg = f"No object with name {self.app_name} found for {self.name} at {self.path}."
            if matches:
                msg += " Did you mean: " + " or ".join(map(repr, matches))
            else:
                msg += " We did find: " + " or ".join(map(repr, options))
            raise NameError(msg)
        return app

    async def watch_app(self):
        from watchgod import awatch

        reload = {
            "type": "reload",
            "reason": "app changed",
        }
        path = self.path
        if str(path).endswith("__init__.py"):
            # if a package, watch the whole directory
            path = path.parent
        print("Watch", path)
        async for changes in awatch(Path(path)):
            print("trigger reload", changes)
            context_values = contexts.values()
            contexts.clear()
            for context in context_values:
                for socket in context.control_sockets:
                    print(socket)
                    await socket.send_json(reload)
            print("send refresh!")


def state_store_all():
    print("Storing state:\n\n\n", list(contexts.keys()))
    for name, context in contexts.items():
        print(f"Storing for {name}")
        context.state_save(state_directory=state_directory)


def state_load(context_name: str):
    path = state_directory / f"{context_name}.pickle"
    if path.exists():
        try:
            with path.open("rb") as f:
                return pickle.load(f)
                # return json.load(f)
        except Exception:
            logger.exception("Failed to load state for context %s", context_name)


atexit.register(state_store_all)
