# Depop Seller

**Turn a shoot's worth of clothing photos into Depop listings.** Your photos are sorted into items,
the descriptions are written in your own style, and Depop's listing form opens with everything
filled in. You check it and press Post.

[![Download for Windows](https://img.shields.io/badge/Download_for_Windows-DepopSellerSetup.exe-4c62cf?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/doyoungp-dev/depop_seller/releases/latest/download/DepopSellerSetup.exe)

Free and open source ([MIT](LICENSE)). It runs on your own computer - there is no account, no
server, and nothing is posted for you.

## What it does

1. **Sort.** Drop in a shoot's photos - hundreds of iPhone photos are fine. Claude groups them into
   items and spots the front, back, label and flaw shots. Anything it gets wrong, you fix by drag
   and drop; the first photo of each item becomes the Depop cover.
2. **Describe.** Tick the items you want and press **Generate Selected Descriptions**. Each one is
   written from the photos plus a line of facts you type - brand, size, material, condition,
   measurements and era or aesthetic hashtags - in your own house style.
3. **List.** The **Depop** button opens Depop's *List an item* page in your own Chrome, with the
   photos in your order and the description already pasted in. Add the price and category, and
   post it yourself.

You can also change how descriptions are written by just saying so ("from now on, add a surf
hashtag group"), keep track of what you have listed, and move everything to another computer by
copying one folder.

## What you need

| | Why | Cost |
|---|---|---|
| A Windows 10 or 11 PC | the app runs there | - |
| [Google Chrome](https://www.google.com/chrome/) | the helper that fills in Depop's form lives in Chrome | free |
| The [Claude desktop app](https://claude.ai/download) with a **Pro or Max** plan | writes the descriptions | your existing subscription |
| An [Anthropic API key](https://console.anthropic.com) with some credit | sorts photos into items | about $2.50 per 440 photos |

## Install

1. **Download** `DepopSellerSetup.exe` - the button above, or **Releases** on the right of this page.
2. **Open it.** Windows may say *Windows protected your PC*: click **More info**, then
   **Run anyway**. It says that about any new app that has not paid for a code-signing
   certificate - which a free app like this one has not.
3. Click **Install**, then **Finish**. Depop Seller opens, and from then on it has an icon on your
   Desktop and in the Start menu. No administrator password is needed, and nothing else - not
   even Python - has to be installed first.
4. **Open the Settings tab** and work down it once: your API key, **Sign in with Claude** (it
   happens in your browser), and the Chrome helper. Each one says what it is for and checks that
   it works.

No black terminal window ever appears, and closing the app window closes the app. To remove it:
Windows **Settings → Apps → Depop Seller → Uninstall** - your data folder stays where it is.

*Would rather not run an installer?* Download `DepopSeller.zip` from the same release instead,
unzip it somewhere you will keep it, and double-click `Depop Seller.cmd`. That route needs
[Python](https://www.python.org/downloads/) 3.12 or newer and sets itself up on first run.

## Using it

| Tab | |
|---|---|
| **Batches** | Name a batch - a date, "Autumn knits", anything - drop its photos on it, press **Group with Claude**. |
| **Review** | Drag photos between items and into order. Delete what you don't want; nothing on disk is ever deleted. |
| **Sell** | Type a line of facts per item, tick items, **Generate Selected Descriptions**. Then **Depop** on each item. Tick **Listed** once it's posted. |
| **Style** | Tell Claude how descriptions should change, see exactly what changes, accept or discard. Every version is kept. |
| **Settings** | Your API key, the Claude sign-in, the Chrome helper, **Check for updates**, your data folder, and **Close Depop Seller**. |

## Your data stays yours

Everything of yours lives in one folder, separate from the app: `%USERPROFILE%\DepopSeller` -
your photos and their grouping, your listings and descriptions, your house style and your API key.

The only things that ever leave your computer:

- photos you choose to **group**, sent to Anthropic with your API key;
- the photos and facts of an item you **generate a description** for, sent to Claude with your
  subscription;
- whatever you **post** yourself on Depop.

Pressing **Check for updates** asks GitHub for the newest version number; nothing of yours is sent.

To use the app on another computer, install it there and copy that one folder across.

## Updating

Open **Settings → Check for updates**. If there is a newer version you will see what changed, and
**Update** installs it - the app closes for a few seconds and reopens by itself. Only the app is
replaced: your photos, listings, style and key live in your data folder, which an update never
touches. The app only contacts GitHub when you press one of those buttons.

Now and then a version also updates the parts the app runs on; the app then says so and points
you to the new `DepopSellerSetup.exe`, which upgrades your copy in place.

## Good to know

- **It never posts for you.** The Depop button fills in the form; pressing Post is always yours.
- **Not affiliated with Depop or Anthropic.** If Depop changes its listing page and the helper
  stops filling it in, please [open an issue](https://github.com/doyoungp-dev/depop_seller/issues).
- **Windows only, for now.** The code includes macOS launchers, but they have not been tested on a
  Mac yet.

---

## For developers

Python 3.12+, standard library HTTP server, vanilla JS pages, a small Chrome extension, and the
Anthropic SDK for photo grouping. Descriptions run through the Claude Code CLI on the user's own
subscription and never touch the API. [`CLAUDE.md`](CLAUDE.md) holds the architecture notes and
the reasoning behind the less obvious choices.

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
```

The tests use tiny synthetic images and never call the API.

Releasing a new version is one command, run from a clean `main`. It runs the tests, sets the
version, commits, tags, builds `DepopSeller.zip` from the committed files and checks it the way the
app's updater will, builds the Windows installer around it and starts the bundled copy to prove it
runs, pushes, and publishes the GitHub release with both files. Every installed copy then offers
it under **Check for updates**.

```bash
.venv\Scripts\python.exe -m depop_seller release --dry-run
.venv\Scripts\python.exe -m depop_seller release
```

The first shows everything that would happen and stops; the second does it (next patch version by
default, or name one: `release 1.1.0`). It needs the [GitHub CLI](https://cli.github.com/) signed
in and [Inno Setup](https://jrsoftware.org/isdl.php) 6, the free installer builder (it can be
installed for the current user only). The installer bundles Python's official embeddable build
with the exact library versions the tests ran against; `installer/DepopSeller.iss` describes it.

### Command line

All commands: `.\.venv\Scripts\python.exe -m depop_seller <command>` from the project folder
(or `depop <command>` after activating the venv). `<batch>` defaults to the newest batch folder.

| Command | What it does |
|---|---|
| `check` | Environment, batches, API key status (only the last 4 characters are shown) |
| `sort <batch>` | Scan + thumbnails, then the Claude grouping (windowed pass + merge pass) → `manifest.csv`. Reuses an existing manifest; `--regroup` redoes it (backup kept) |
| `sort <batch> --engine local` | Offline fallback grouping (time gaps + colour); over-splits, no API cost |
| `sort <batch> --limit 24 --regroup` | Cheap test on the first 24 photos |
| `sort <batch> --model claude-sonnet-5` | Use another model (`--effort low/medium/high` too) |
| `sort <batch> --no-merge-pass` | Skip the second pass that finds re-shoots of earlier items |
| `sort <batch> --apply` | Write `sort_image/` + database from the reviewed manifest (`--force` to replace) |
| `hub` | Start the app on the Batches tab (what the launcher does) |
| `review [<batch>]` | Start the server and open the review page (`--port`, `--no-browser`) |
| `sell [<batch>]` | Start the server and open the Sell page (Depop button per item; photos only, never posts) |
| `learn <batch>` | Score the engine's proposal against your reviewed manifest (free) |
| `learn <batch> --test-merge-pass` | Re-run the merge pass on the original proposal and score it (calls the API) |
| `python -m pytest -q` | Run the tests (synthetic images, no API calls) |
| `release [X.Y.Z] [--dry-run]` | For maintainers: test, tag and publish a new version (see above) |

Every API response is cached under `<data folder>\product_image\<batch>\cache\`, so re-running a
command never pays twice for the same photos; only `--regroup` after a prompt change does.

### How grouping works

1. **Windowed pass** — thumbnails are sent to Claude eight at a time (plus two context photos
   from the previous window) with the time gap between shots; the model says whether each photo
   shows the same item as the previous one, labels its view, and flags non-product photos.
2. **Merge pass** — a catalog of every item's cover photo is sent (prompt-cached) with a few
   query items per call; the model reports which items are re-shoots of each other. High
   confidence merges automatically; medium becomes a suggestion on the review page.

On a real 438-photo batch (101 proposed groups) the windowed pass made zero boundary
mistakes and the merge pass found all 7 re-shoots the reviewer had merged by hand plus 38 more
that the reviewer had missed; `learn` prints these numbers for any batch you have reviewed.

## Licence

MIT - see [LICENSE](LICENSE).
