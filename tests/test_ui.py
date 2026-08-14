import curses
from unittest.mock import MagicMock, Mock, patch

import pytest

from kirk.client import Buffer, IrcClient, IrcRawMessage
from kirk.kirk import ExitInterrupt, Kirk, Window


def test_window_creation():
    buf = Buffer[IrcRawMessage]()
    window = Window("testwindow", buf)

    assert window.name == "testwindow"
    assert window.buf is buf
    assert window.buf_idx_viewed == 0
    assert window.buf_idx_frozen is None
    assert window.page == 0


def test_window_page_up():
    window = Window("test", Buffer[IrcRawMessage]())

    window.page_up()
    assert window.page == 1
    assert window.buf_idx_frozen == 0

    # further page ups keep the buffer frozen at its initial value
    window.page_up()
    assert window.page == 2
    assert window.buf_idx_frozen == 0


def test_window_page_down():
    window = Window("test", Buffer[IrcRawMessage]())

    window.page_up()
    window.page_up()
    window.page_down()
    assert window.page == 1

    # returning to page 0 unfreezes the buffer
    window.page_down()
    assert window.page == 0
    assert window.buf_idx_frozen is None


def test_window_page_down_minimum():
    window = Window("test", Buffer[IrcRawMessage]())
    window.page_down()
    assert window.page == 0


def test_window_page_reset():
    window = Window("test", Buffer[IrcRawMessage]())
    window.page_up()
    window.page_up()

    window.page_reset()
    assert window.page == 0
    assert window.buf_idx_frozen is None


def test_window_dirty_view():
    buf = Buffer[IrcRawMessage]()
    window = Window("test", buf)

    assert not window.dirty_view

    buf.insert(IrcRawMessage("test", "PRIVMSG", ["#test", "hello"]))
    assert window.dirty_view

    # scrolled away from the bottom, new messages shouldn't mark the view dirty
    window.page_up()
    assert not window.dirty_view


def test_window_dirty_buf():
    buf = Buffer[IrcRawMessage]()
    window = Window("test", buf)

    assert not window.dirty_buf

    buf.insert(IrcRawMessage("test", "PRIVMSG", ["#test", "hello"]))
    assert window.dirty_buf

    window.reset_buf()
    assert not window.dirty_buf


def test_window_get_buf_page():
    buf = Buffer[IrcRawMessage]()
    window = Window("test", buf)

    for i in range(10):
        buf.insert(IrcRawMessage("test", "PRIVMSG", ["#test", f"message {i}"]))

    page = window.get_buf_page(5)
    assert len(page) <= 5
    # most recent message first (LIFO)
    assert page[0].params[1] == "message 9"


def test_kirk_initialization():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)

        assert kirk.clients == [client]
        assert kirk.client_idx == 0
        assert kirk.current_window_name == client.server_buf_name
        assert kirk.dirty
        assert kirk.error_msg == ""


def test_kirk_client_property():
    client1 = IrcClient(host="server1.com", nick="nick1")
    client2 = IrcClient(host="server2.com", nick="nick2")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client1, client2], loop)
        assert kirk.client is client1

        kirk.client_idx = 1
        assert kirk.client is client2


def test_kirk_switch_client():
    client1 = IrcClient(host="server1.com", nick="nick1")
    client2 = IrcClient(host="server2.com", nick="nick2")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client1, client2], loop)

        kirk.switch_client()
        assert kirk.client_idx == 1
        assert kirk.current_window_name == "server2.com"

        # wraps around
        kirk.switch_client()
        assert kirk.client_idx == 0


def test_kirk_switch_window_relative():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.sync_client()  # populate windows, including the server window
        kirk.windows["#channel1"] = Window("#channel1", Buffer())
        kirk.windows["#channel2"] = Window("#channel2", Buffer())

        assert kirk.current_window_name == "test.com"

        kirk.switch_window_relative(1)
        assert kirk.current_window_name in ["#channel1", "#channel2"]


def test_kirk_parse_prompt_command():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.prompt_buf = list("/join #test")
        assert kirk.parse_prompt() == ("join", ["#test"])


def test_kirk_parse_prompt_message():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.prompt_buf = list("Hello world")
        assert kirk.parse_prompt() == ("", ["Hello", "world"])


def test_kirk_parse_prompt_empty():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.prompt_buf = []
        assert kirk.parse_prompt() == ("", [])


def test_kirk_is_server_window():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        assert kirk.is_server_window

        kirk.windows["#test"] = Window("#test", Buffer())
        kirk.current_window_name = "#test"
        assert not kirk.is_server_window


def test_kirk_process_input_page_up_down():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.sync_client()

        keystroke = Mock()
        keystroke.code = curses.KEY_PPAGE
        kirk.process_input(keystroke)
        assert kirk.current_window.page == 1

        keystroke.code = curses.KEY_NPAGE
        kirk.process_input(keystroke)
        assert kirk.current_window.page == 0


def test_kirk_process_input_arrow_keys():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.windows["#test"] = Window("#test", Buffer())

        keystroke = Mock()
        keystroke.code = curses.KEY_RIGHT
        with patch.object(kirk, "switch_window_relative") as mock_switch:
            kirk.process_input(keystroke)
            mock_switch.assert_called_once_with(offset=1)

        keystroke.code = curses.KEY_LEFT
        with patch.object(kirk, "switch_window_relative") as mock_switch:
            kirk.process_input(keystroke)
            mock_switch.assert_called_once_with(offset=-1)


def test_kirk_process_input_backspace():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.prompt_buf = list("hello")
        kirk.error_msg = "some error"

        keystroke = Mock()
        keystroke.code = curses.KEY_BACKSPACE
        kirk.process_input(keystroke)

        assert kirk.prompt_buf == list("hell")
        assert kirk.error_msg == ""


def test_kirk_process_input_delete():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.prompt_buf = list("hello world")

        keystroke = Mock()
        keystroke.name = "KEY_DELETE"
        kirk.process_input(keystroke)

        assert kirk.prompt_buf == []


def test_kirk_process_input_character():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)

        keystroke = MagicMock()
        keystroke.code = None
        keystroke.name = ""
        keystroke.is_sequence = False
        keystroke.__str__.return_value = "a"  # type: ignore[attr-defined]

        kirk.process_input(keystroke)
        assert kirk.prompt_buf == ["a"]


def test_kirk_process_input_enter():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.prompt_buf = list("/quit")

        keystroke = Mock()
        keystroke.code = curses.KEY_ENTER
        with patch.object(kirk, "process_prompt") as mock_process:
            kirk.process_input(keystroke)
            mock_process.assert_called_once()


def test_kirk_process_prompt_join():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.prompt_buf = list("/join #testchannel")

        with patch.object(client, "delay") as mock_delay:
            kirk.process_prompt()
            mock_delay.assert_called_once()
            mock_delay.call_args[0][0].close()  # avoid "coroutine was never awaited" from the mock


def test_kirk_process_prompt_exit():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.prompt_buf = list("/exit")

        with patch.object(client, "delay") as mock_delay:
            with pytest.raises(ExitInterrupt):
                kirk.process_prompt()

            mock_delay.assert_called_once()
            mock_delay.call_args[0][0].close()


def test_kirk_process_prompt_message_to_channel():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.windows["#test"] = Window("#test", Buffer())
        kirk.current_window_name = "#test"
        kirk.prompt_buf = list("Hello everyone!")

        with patch.object(client, "delay") as mock_delay:
            kirk.process_prompt()
            mock_delay.assert_called_once()
            mock_delay.call_args[0][0].close()
            assert kirk.prompt_buf == []


def test_kirk_process_prompt_message_to_server():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        assert kirk.is_server_window
        kirk.prompt_buf = list("Hello server")

        kirk.process_prompt()

        assert "Cannot send message to server window" in kirk.error_msg
        assert kirk.prompt_buf == []


def test_kirk_sync_client():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)

        channel = client.channels["#testchannel"]
        channel.topic = "Test topic"
        client.chats["friend"].insert(IrcRawMessage("friend", "PRIVMSG", ["testnick", "hi"]))

        kirk.sync_client()

        assert "#testchannel" in kirk.windows
        assert "friend" in kirk.windows
        assert kirk.windows["#testchannel"].header == "[#testchannel] Test topic"
        assert kirk.windows["friend"].header == "[friend]"


def test_opt_number_mapping():
    from kirk.kirk import OPT_NUMBER_MAPPING

    assert len(OPT_NUMBER_MAPPING) == 10
    assert OPT_NUMBER_MAPPING["¡"] == 0
