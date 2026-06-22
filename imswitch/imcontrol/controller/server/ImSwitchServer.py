import asyncio
import inspect
import threading
import Pyro5
import Pyro5.server
from imswitch.imcommon.framework import Worker
from imswitch.imcommon.model import initLogger
from ._serialize import register_serializers
from fastapi import FastAPI
import uvicorn
from functools import wraps

app = FastAPI()


class ImSwitchServer(Worker):

    def __init__(self, api, setupInfo):
        super().__init__()

        self.__logger = initLogger(self, tryInheritParent=True)
        self._api = api
        self._name = setupInfo.pyroServerInfo.name
        self._host = setupInfo.pyroServerInfo.host
        self._port = setupInfo.pyroServerInfo.port

        self._paused = False
        self._canceled = False
        self._uvicorn_server = None
        self._pyro_daemon = None
        self._pyro_thread = None
        self._uvicorn_loop = None

    def run(self):
        self.createAPI()
        
        # Start FastAPI/uvicorn server in non-blocking mode on configured host/port
        # Use port+1000 for uvicorn to avoid conflict with Pyro daemon
        uvicorn_port = self._port + 1000
        config = uvicorn.Config(
            app,
            host=self._host,
            port=uvicorn_port,
            log_level="info"
        )
        self._uvicorn_server = uvicorn.Server(config)
        
        # Run uvicorn in a separate thread with its own event loop
        def run_uvicorn():
            self._uvicorn_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._uvicorn_loop)
            try:
                serveResult = self._uvicorn_server.serve()
                if inspect.isawaitable(serveResult):
                    self._uvicorn_loop.run_until_complete(serveResult)
            finally:
                self._uvicorn_loop.close()
        
        uvicorn_thread = threading.Thread(target=run_uvicorn, daemon=True)
        uvicorn_thread.start()
        
        self.__logger.debug(f"Started FastAPI server at {self._host}:{uvicorn_port}")
        self.__logger.debug("Started server with URI -> PYRO:" + self._name + "@" + self._host + ":" + str(self._port))
        
        # Start Pyro server on configured port
        try:
            Pyro5.config.SERIALIZER = "msgpack"
            register_serializers()

            self._pyro_daemon = Pyro5.server.Daemon(host=self._host, port=self._port)
            self._pyro_daemon.register(self, self._name)
            self.__logger.debug(f"Pyro daemon registered at {self._host}:{self._port}")
            self._pyro_daemon.requestLoop()

        except Exception:
            self.__logger.exception("Couldn't start server.")
        self.__logger.debug("Loop Finished")

    def stop(self):
        """Stop both uvicorn and Pyro servers. Idempotent."""
        if self._uvicorn_server is not None:
            uvicorn_server = self._uvicorn_server
            try:
                uvicorn_server.should_exit = True
                if self._uvicorn_loop is not None and not self._uvicorn_loop.is_closed():
                    self._uvicorn_loop.call_soon_threadsafe(
                        lambda server=uvicorn_server: setattr(server, 'should_exit', True)
                    )
            except Exception as e:
                self.__logger.warning(f"Error stopping uvicorn server: {e}")
            finally:
                self._uvicorn_server = None
        
        if self._pyro_daemon is not None:
            try:
                self._pyro_daemon.shutdown()
            except Exception as e:
                self.__logger.warning(f"Error stopping Pyro daemon: {e}")
            finally:
                self._pyro_daemon = None

    @app.get("/")
    def createAPI(self):
        api_dict = self._api._asdict()
        functions = api_dict.keys()

        def includeAPI(str, func):
            @app.get(str)
            @wraps(func)
            async def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            return wrapper

        def includePyro(func):
            @Pyro5.server.expose
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            return wrapper

        for f in functions:
            func = api_dict[f]
            if hasattr(func, 'module'):
                module = func.module
            else:
                module = func.__module__.split('.')[-1]
            self.func = includePyro(includeAPI("/"+module+"/"+f, func))


# Copyright (C) 2020-2022 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
