"""Static help text shown by the /help command."""

HELP_TEXT = """\
Kirk commands:
    /help, /h              show this help
    /exit                  quit kirk and disconnect all clients
    /quit, /q              disconnect the current client
    /switch                switch to the next client (i.e. IRC server)
    /join, /j <channel>    join a channel
    /part                  leave the current channel
    /close                 close the current window/tab
    /msg, /m <nick> <text> send a private message
    /me <text>             send an action message to the current window
    /ctcp <target> <text>  send a raw CTCP request
    /handshake <nick>      start a secure DH key exchange
    /whois, /w <nick>      look up a user
    /list                  list channels on the server
    /members               list members of the current channel
    /nick <name>           change your nickname
    /grep <term>           open a filtered window of matching messages
    /raw <cmd> [args]      send a raw IRC command
    /dcc send/ssend <nick> <file> offer a file to a user over DCC (ssend = TLS)
    /save                  persist current session state

Navigation:
    Left/Right arrows      switch tabs
    Page Up/Down           scroll the current window
    End                    jump to the bottom of the window

Any other text is sent as a message to the current window.
"""
