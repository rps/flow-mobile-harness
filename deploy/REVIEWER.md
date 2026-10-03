# Using the harness web UI (reviewer guide)

You need: a Mac or Linux machine with a terminal and a browser, the `reviewer_key` file you were
sent, and this document. Nothing to install, no account to create.

## 1. Open the tunnel

```sh
chmod 600 reviewer_key
ssh -i reviewer_key -N -L <PORT>:127.0.0.1:<PORT> reviewer@<VM_IP>
```

The first time, ssh asks `Are you sure you want to continue connecting?` — answer `yes`. The
command then prints nothing and keeps running: that is the tunnel. Leave this window open.

## 2. Open the UI

In your browser:

```
http://127.0.0.1:<PORT>/?token=<TOKEN>
```

After the first load the token is remembered in a cookie, so `http://127.0.0.1:<PORT>/` is enough.

## 3. Run tasks

Pick a task, leave **Confirmation** on "Always approve" (the default) so flows run end to end
without you having to answer prompts, and press Start. Runs are real: they drive an Android
emulator on the VM and call the model, so each takes a few minutes and spends real money. Runs
execute one at a time; a second Start waits in the queue. The scoreboard and per-run traces update
live.

"Ask me here (production recommended)" instead routes each sensitive step to you for approval
in the page; unanswered prompts reject after the timeout shown in the page. Tick **Fake** to
exercise the UI without the emulator or the model.

## 4. Finish

Ctrl-C in the terminal closes the tunnel. Nothing else to clean up.

## If something fails

| You see                                       | Meaning                                                                                                              |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `Connection refused` / `Connection timed out` | the VM is not running; tell the owner                                                                                |
| `Permission denied (publickey)`               | wrong key file, or `chmod 600 reviewer_key` not done                                                                 |
| `{"detail":"token required"}` in the browser  | the token in the URL is wrong or missing                                                                             |
| `channel ... open failed`                     | something else on your machine already uses port <PORT>; change the first `<PORT>` in the ssh command and in the URL |
| `IN USE: <name> is running ...` banner        | another tester is using the one device; your job queues behind theirs and starts by itself, so wait (the banner shows your queue position) |
