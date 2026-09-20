# Running the call server in the cloud

The call server has to be reachable from anywhere and always on, so she
can ring your phone while you're out and your laptop is off. This is the
Azure-for-Students path (free with a student email, no credit card); the
same steps work on any Ubuntu VM.

Everything below happens once. Afterwards the server restarts by itself
on reboot or crash.

## 1. Create the VM (Azure portal)

**Virtual machines -> Create -> Azure virtual machine**

| Field | Value |
|---|---|
| Subscription | Azure for Students |
| Resource group | Create new: `heywaifu` |
| Region | **Central India** (close = better call latency) |
| Image | **Ubuntu Server 24.04 LTS - x64 Gen2** |
| Size | **B1s** (1 vCPU, 1 GiB) -- about $7.50/month against your $100 |
| Authentication | SSH public key, **Generate new key pair** |
| Inbound ports | SSH (22), HTTP (80), HTTPS (443) |

Create it and download the private key (`heywaifu_key.pem`) when asked --
it's shown only once.

Then give it a permanent address and name:

- The VM's **Public IP address** -> Configuration -> set **Assignment:
  Static**, and set a **DNS name label** such as `heywaifu`.
- That gives you a hostname like
  `heywaifu.centralindia.cloudapp.azure.com`. Use it everywhere below.

Also check **Operations -> Auto-shutdown** is *disabled*: an off VM can't
call you.

## 2. Connect

From PowerShell on your PC, in the folder with the key:

```powershell
icacls heywaifu_key.pem /inheritance:r /grant:r "$($env:USERNAME):(R)"
ssh -i heywaifu_key.pem azureuser@heywaifu.centralindia.cloudapp.azure.com
```

## 3. Set it up

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/HiiiiiPritam/Wifu_v1.git
cd Wifu_v1
bash deploy/setup.sh heywaifu.centralindia.cloudapp.azure.com
```

It will stop and ask for your keys. In another PowerShell window, copy the
Firebase key up from your PC:

```powershell
scp -i heywaifu_key.pem "C:\...\phase1\firebase-adminsdk.json" azureuser@heywaifu.centralindia.cloudapp.azure.com:~/Wifu_v1/phase1/
```

Back in the SSH session, paste your Groq key into `phase1/.env`
(`nano phase1/.env`, then Ctrl+O, Enter, Ctrl+X) and re-run:

```bash
bash deploy/setup.sh heywaifu.centralindia.cloudapp.azure.com
```

It prints the URL to paste into the app.

## 4. Optional: bring her memory along

Otherwise she starts fresh (and her settings go back to defaults):

```powershell
scp -i heywaifu_key.pem "C:\...\phase1\memory.json" "C:\...\phase1\settings.json" azureuser@HOST:~/Wifu_v1/phase1/
```

Then `sudo systemctl restart heywaifu`.

## 5. Point the app at it

In the app: **Settings -> Connection**, replace the address with the URL
from step 3, then **Save & register**. Her photo uploads again from
Settings -> Her -> Change photo.

## Everyday commands

```bash
sudo systemctl restart heywaifu     # after changing settings/keys
journalctl -u heywaifu -f           # live log (what she's doing)
journalctl -u heywaifu -n 50        # last 50 lines
tail -f ~/Wifu_v1/phase1/call_logs/$(date +%F).txt   # call transcripts
git -C ~/Wifu_v1 pull && sudo systemctl restart heywaifu   # update
```

## Notes

- **The secret in the URL is the only thing protecting the server** (see
  `phase1/call/access.py`). Don't paste that URL anywhere public. To
  change it: `rm phase1/.call_token`, restart, and paste the new URL in
  the app.
- **Keep an eye on the credit**: Cost Management -> Budgets, set an alert
  at ~$80 so a year's credit doesn't run out unnoticed.
- **Speech-to-text is Groq-only here** (`requirements-server.txt` skips
  the local model, which needs ~200MB and more RAM than this VM has).
- The desktop companion still runs on your PC and now keeps its own
  memory of you, separate from the server's.
