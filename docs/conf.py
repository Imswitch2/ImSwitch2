import os
import sys

from sphinx.ext import autodoc

# The documented modules are imported from the source tree, not from an
# installed package: Read the Docs installs only docs/requirements-readthedocs.txt.
sys.path.insert(0, os.path.abspath('..'))

extensions = ['sphinx.ext.autodoc', 'sphinx.ext.napoleon']

# Nothing is mocked: the framework layer combines Qt classes with its own
# metaclass, which Sphinx's mock objects cannot stand in for, so the docs
# builder installs a real PyQt5 instead (docs/requirements-readthedocs.txt).
autodoc_mock_imports = []

html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']
html_css_files = ['css/custom.css']
html_logo = 'images/imswitch2_logo.png'
html_favicon = 'images/imswitch2_icon.png'

project = 'Imswitch2'
copyright = '2020-2026, ImSwitch developers'


class ClassCondensedHeaderDocumenter(autodoc.ClassDocumenter):
    """ Class documenter that only prints out the class name in the header. """

    objtype = 'classconheader'

    def add_directive_header(self, sig):
        sourcename = self.get_sourcename()
        self.add_line(f'.. class:: {self.format_name()}', sourcename)
        # The base implementation emits these from the options; this override
        # replaces it wholesale, so re-emit the one that decides whether a
        # second description of the same class is a duplicate.
        if self.options.get('no-index') or self.options.get('noindex'):
            self.add_line('   :no-index:', sourcename)


def setup(app):
    app.add_autodocumenter(ClassCondensedHeaderDocumenter)
