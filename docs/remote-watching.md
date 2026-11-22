# Watching lanes from anywhere: Tailscale + herdr + Heeler

`agent-lanes` itself only needs ssh. This page is what we learned running lanes on two Windows/WSL machines and a
Mac, and checking on them from a phone. None of it is required; all of it cost us time once.

| Piece | What it is | What it does for lanes |
|---|---|---|
| Tailscale | Private mesh network between your own devices | Every machine and the phone see each other; nothing is opened to the internet |
| herdr | Terminal workspace server + TUI, one per machine | Each lane worker gets its own tab, so you can watch it work and see when it is done |
| Heeler | iOS / iPadOS / macOS client for herdr over SSH | The same tabs on a phone, plus a push notification when a pane finishes |

```
 phone (Heeler) --ssh over Tailscale--> PC     : herdr  [ lead session | lanes: oc-worker-07 | oc-worker-08 ]
                                     \-> laptop : herdr  [ lanes: oc-worker-10 (remote lane, systemd unit) ]
```

## Tailscale

- **Close sshd to everything but the tailnet.** On Windows the Tailscale adapter is a Private network; allow the
  OpenSSH rule on Private only and disable any broad "sshd" rules a past install left behind.
- **Publickey only, everywhere.** Turn off `PasswordAuthentication` *and* `KbdInteractiveAuthentication`; a host
  that only disables passwords still offers keyboard-interactive.
- **Restart remote sshd so a mistake cannot lock you out.** Validate the config first (`sshd -t`), then restart
  from a one-shot scheduled task / unit that rolls back if the new config does not come up, not from inside the
  ssh session you are using.
- **Some networks cannot reach your tailnet** (guest, corporate, captive Wi-Fi). Use `--host-or-local` for
  `lane run` / `lane queue` so a lane falls back to local, and `lane collect` later from a network that works.
- **An app's built-in Tailscale is a separate node.** If a client has its own Tailscale login, it shows up as a new
  device: add it to your ACLs like any other.

## WSL hosts

- **Land directly in Linux.** Run a user-level sshd inside WSL on `127.0.0.1:2222` (no sudo needed) and let the
  Windows sshd be a jump host: `ssh -J win-host -p 2222 you@127.0.0.1`. With mirrored networking Windows reaches
  WSL's localhost directly. This is much simpler than a forced `wsl.exe` command.
- **Forced commands and ControlMaster do not mix.** A Windows sshd `command="wsl.exe ..."` key plus ssh
  multiplexing re-ran the *first* session's command for every later session.
- **Linger.** Without `loginctl enable-linger` (needs root), the WSL user manager stops after a WSL restart, and
  with it the user sshd, herdr and any remote lane units. Someone has to open WSL once before the phone can attach.
- **Remote lanes need a supervisor.** A `setsid nohup` lane died when the ssh session that started it closed; a
  `systemd --user` unit (at `nice 19`) survived. `lane host check` tests for `systemd --user`.
- **Non-interactive ssh has a different PATH.** Put PATH lines *above* the "not interactive, return" guard in
  `~/.bashrc` (Linux) or in `~/.zshenv` (macOS/Homebrew), or `ssh host lane ...` cannot find the tools.
- **Keep shell pipes out of the remote command line** when the first hop is a Windows shell: pipe locally instead.

## herdr

- **One workspace per topic**, each started in its own folder. Claude Code keys its memory and project
  instructions by working directory, so a topic folder with its own `CLAUDE.md` keeps sessions from bleeding into
  each other. A shared facts file works best as a user-level rules file: `@imports` that point outside the
  project folder (symlinks included) were skipped silently.
- **Lanes in a `lanes` workspace.** We start each local worker in its own herdr tab. The lane tool stays *outside*
  the worker's sandbox and reports the pane as working, then idle, so herdr marks it done.
- **Never give a worker the herdr socket.** It can type into any pane, including the lead's session. Keep it out
  of the sandbox even when an agent integration asks for it.
- **Do not run long-lived critical processes inside herdr** (services, anything that must survive a terminal
  server restart). Lanes are fine; they are disposable and recorded in the ledger.

## Heeler

- **Client, not server.** herdr runs next to the agents; Heeler is the app on the device in your pocket. It
  connects over SSH (through the jump host above) and shows herdr's workspaces and panes.
- **Notifications come from a herdr plugin** on each host (it needs Node 20+). Pushes go through a relay
  end-to-end encrypted. Check `herdr plugin log list` when they stop arriving.
- **Live terminal attach is exclusive per pane**: attaching from the phone takes the pane over from the desktop.
  The Chat view is shared, so use it for watching.
- **One key per device; match keys by body, not by label.** Two of our devices generated keys with the same
  label; only the key body tells them apart in `authorized_keys`.
- **Plain SSH apps work too** (`ssh -J` to the WSL sshd, then `herdr`); Heeler adds the herdr-aware view and the
  push.

## What it bought us

- Lanes are visible while they run, not only when `lane run` returns.
- A finished lane pings the phone, so review starts sooner and the queue does not sit idle.
- Nothing listens on the public internet: every hop is Tailscale + publickey SSH.
