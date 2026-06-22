from importlib import import_module

# Keep the package attribute module-shaped so dotted patch/import paths such as
# ``imswitch.imcontrol.controller.server.ImSwitchServer.Pyro5`` resolve to the
# actual module instead of a class exported from this package.
ImSwitchServer = import_module(f'{__name__}.ImSwitchServer')
ImSwitchServerClass = ImSwitchServer.ImSwitchServer

__all__ = ['ImSwitchServer', 'ImSwitchServerClass']
