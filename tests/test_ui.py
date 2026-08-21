import curses
from unittest.mock import Mock, patch

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
        assert kirk.parse_prompt("/join #test") == ("join", ["#test"])


def test_kirk_parse_prompt_message():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        assert kirk.parse_prompt("Hello world") == ("", ["Hello", "world"])


def test_kirk_parse_prompt_empty():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        assert kirk.parse_prompt("") == ("", [])


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
        keystroke.name = "KEY_PGUP"
        kirk.process_input(keystroke)
        assert kirk.current_window.page == 1

        keystroke.name = "KEY_PGDOWN"
        kirk.process_input(keystroke)
        assert kirk.current_window.page == 0


def test_kirk_process_input_shift_arrow_switches_tabs():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.windows["#test"] = Window("#test", Buffer())

        keystroke = Mock()
        keystroke.name = "KEY_SRIGHT"
        with patch.object(kirk, "switch_window_relative") as mock_switch:
            kirk.process_input(keystroke)
            mock_switch.assert_called_once_with(offset=1)

        keystroke.name = "KEY_SLEFT"
        with patch.object(kirk, "switch_window_relative") as mock_switch:
            kirk.process_input(keystroke)
            mock_switch.assert_called_once_with(offset=-1)


def test_kirk_process_prompt_records_history():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.windows["#test"] = Window("#test", Buffer())
        kirk.current_window_name = "#test"

        enter = Mock()
        enter.name = "KEY_ENTER"

        kirk.e.insert_text("Hello everyone!")
        with patch.object(client, "delay") as mock_delay:
            kirk.process_input(enter)
            mock_delay.call_args[0][0].close()

        assert kirk.e.history.entries == ["Hello everyone!"]

        # submitting the exact same line again is not duplicated
        kirk.e.insert_text("Hello everyone!")
        with patch.object(client, "delay") as mock_delay:
            kirk.process_input(enter)
            mock_delay.call_args[0][0].close()

        assert kirk.e.history.entries == ["Hello everyone!"]


def test_kirk_process_input_enter():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.e.insert_text("/quit")

        keystroke = Mock()
        keystroke.name = "KEY_ENTER"
        keystroke.code = curses.KEY_ENTER
        with patch.object(kirk, "process_prompt") as mock_process:
            kirk.process_input(keystroke)
            mock_process.assert_called_once_with("/quit")


def test_kirk_process_prompt_join():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)

        with patch.object(client, "delay") as mock_delay:
            kirk.process_prompt("/join #testchannel")
            mock_delay.assert_called_once()
            mock_delay.call_args[0][0].close()  # avoid "coroutine was never awaited" from the mock


def test_kirk_process_prompt_exit():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)

        with patch.object(client, "delay") as mock_delay:
            with pytest.raises(ExitInterrupt):
                kirk.process_prompt("/exit")

            mock_delay.assert_called_once()
            mock_delay.call_args[0][0].close()


def test_kirk_process_prompt_message_to_channel():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        kirk.windows["#test"] = Window("#test", Buffer())
        kirk.current_window_name = "#test"

        with patch.object(client, "delay") as mock_delay:
            kirk.process_prompt("Hello everyone!")
            mock_delay.assert_called_once()
            mock_delay.call_args[0][0].close()


def test_kirk_process_prompt_message_to_server():
    client = IrcClient(host="test.com", nick="testnick")
    loop = Mock()

    with patch("kirk.kirk.Terminal"):
        kirk = Kirk([client], loop)
        assert kirk.is_server_window

        kirk.process_prompt("Hello server")

        assert "Cannot send message to server window" in kirk.error_msg


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
