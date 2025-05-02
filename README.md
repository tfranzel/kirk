<img alt="kirk.png" height="400px" src="kirk.png"/>

# Kirk

Another IRC client & UI, because [Irssi](https://github.com/irssi/irssi) is great but painful to script.
[gawel/irc3](https://github.com/gawel/irc3) works, but its design flaws are limiting and hard to work around.
[jaraco/irc](https://github.com/jaraco/irc) is weirdly over-engineered while making seemingly simple things very hard. 

This is a minimalistic, portable, yet almost complete
[rfc2812](https://datatracker.ietf.org/doc/html/rfc2812#section-3.7.3)
implementation (~600 LOC client, ~400 LOC UI), which I should have written years ago, instead of
fighting above tools.

## UI features

* Irssi inspired UI (powered by [blessed](https://github.com/jquast/blessed) ♥️)
* Colorized nicks
* MIRC color/formatting
* Tabbed layout (`ARROW_LEFT`, `ARROW_RIGHT`)
* Scrollable buffers (`PAGE_UP`, `PAGE_DOWN`)
* Multi-server enabled (switch through with `/s`)

## Client features

* Fully `asyncio`
* Implements most of [`rfc2812`](https://datatracker.ietf.org/doc/html/rfc2812#section-3.7.3)
* Symmetric E2E encryption with `cryptography.Fernet` (Kirk exclusive feature)
* Auto-join
* Auth support (via NickServ)
* CTCP support
* DCC support
* SSL support
* Super easy to extend and adapt!

## Demo

Super simple IRC bot [ollama_chatbot.py](examples/ollama_chatbot.py) that reacts to private messages. Uses a locally running ollama LLM.

![demo.gif](demo.gif)

## Running it

By default, Kirk will look into the user's home directory for a startup config file.
This is just a convenience. Running it yourself is also easy, see [run.py](kirk/run.py).

### Start UI

```bash
python -m kirk.run
```

### Non-exhaustive example for config file `~/.kirk.toml`

```toml
[kirk]
# save buffer history on exit and load on start
persistence = false
# Custom IrcClient class that Kirk should use instead.
# client_class = "some.import.path"

[[kirk.client]]
host = "irc.libera.chat"
nick = "Uhura"
auto_join = [
    "#testchannel",
]
ssl = true
# Optional: AuthServe authentication
auth = "YOURPASSWORD"
# Symmentric encrpytion with peer. Both need the same key
# Key generation: base64.urlsafe_b64encode(os.urandom(32))
keys.CpnKirk25 = "R3k4JBrPet51XYzkD64KNeuOljzANxBT6yIHZT-yY7w="
# Use "console" with headless and "file" or "none" for Kirk
log_mode = "none"

# another server
[[kirk.client]]
# ...
```

### Running it manually

```python
import asyncio

from kirk.client import IrcClient
from kirk.kirk import Kirk

async def main() -> None:
    loop = asyncio.get_running_loop()
    client = IrcClient(
        host="irc.libera.chat",
        nick="YourName",
    )
    kirk = Kirk([client], loop)
    # UI is select()-based, so run in a separate thread
    _ = loop.run_in_executor(None, kirk.run)
    # start the actual client
    await asyncio.gather(client.run())


if __name__ == "__main__":
    asyncio.run(main(), debug=False)
```
