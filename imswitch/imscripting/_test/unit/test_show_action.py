"""show(): an array to ImControl's image view through the module channel,
copied, named, one layer per entry of a dict; a clear error when nothing
can show it."""
import numpy as np
import pytest

from imswitch.imcommon.controller import ModuleCommunicationChannel
from imswitch.imscripting.model import getActionsScope


@pytest.fixture
def channel(qtbot):
    return ModuleCommunicationChannel()


def _show(channel):
    return getActionsScope({'moduleCommChannel': channel})['show']


def test_show_sends_a_copy_with_its_name_and_scale(channel):
    received = []
    channel.sigShowImage.connect(lambda *args: received.append(args))
    image = np.arange(6, dtype=np.uint16).reshape(2, 3)

    _show(channel)(image, 'mean', scale=(0.1, 0.1))
    image[0, 0] = 99

    (name, shown, scale), = received
    assert name == 'mean' and scale == (0.1, 0.1)
    assert shown[0, 0] == 0                       # a copy: later changes do not show


def test_show_names_a_layer_script_by_default_and_one_per_dict_entry(channel):
    received = []
    channel.sigShowImage.connect(lambda *args: received.append(args[0]))
    show = _show(channel)
    show(np.zeros((2, 2)))
    show({'Camera': np.zeros((2, 2)), 'APD': np.zeros((3, 3))})
    show({'Camera': np.zeros((2, 2))}, 'scan')
    assert received == ['script', 'Camera', 'APD', 'scan: Camera']


def test_show_refuses_a_1d_array(channel):
    channel.sigShowImage.connect(lambda *args: None)
    with pytest.raises(ValueError, match='two or more dimensions'):
        _show(channel)(np.zeros(5))


def test_show_says_when_nothing_can_show_the_image(channel):
    with pytest.raises(RuntimeError, match='Image widget'):
        _show(channel)(np.zeros((2, 2)))
