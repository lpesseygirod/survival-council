# Putting Survival Council on its server

About 30 minutes, once. Everything here is done by you; after that the server runs on its own.

You need: a server (below), a domain or subdomain you control (for example `council.yourdomain.com`), and your Mac's terminal.

**Who pays the server is your choice** (setup wizard, step 3, "Its server"):
- **Free for it**: you provide the server (for example with cloud credits you already have). Rent is not part of its world: RULES.md doesn't mention it and nothing is charged to its Wallet.
- **Its card pays**: the rent is in its rules, the watchdog deducts it from its Wallet, and if the card can't pay, the server stops.

Any provider works if it gives you **Ubuntu 24.04 with about 4 GB of memory** and lets you open port 443 and restrict port 22. The Hetzner steps below are one example; with Azure, AWS or Google Cloud the same choices exist under different names (image, size, SSH key, network security group / firewall). Turn backups and snapshots off: death means no backup copy.

## 1. An SSH key on your Mac

Skip this if `ls ~/.ssh/id_ed25519.pub` already shows a file.

```bash
ssh-keygen -t ed25519 -C "survival-council"
```

Press Enter for the default location and choose a passphrase. Then copy the public key:

```bash
pbcopy < ~/.ssh/id_ed25519.pub
```

## 2. The server on Hetzner

1. Create an account at <https://console.hetzner.cloud>. Under **Billing → Payment method**, add **its virtual card** if its card pays the rent (otherwise your own).
2. **New project** → name it `survival-council`.
3. **Security → SSH keys → Add SSH key**: paste the key you copied.
4. **Firewalls → Create firewall**, name `council`:
   - Inbound **TCP 443** from **Any IPv4 / Any IPv6** (the dashboard)
   - Inbound **TCP 22** from **your own IP only** (look it up at <https://ifconfig.me>, enter it as `x.x.x.x/32`)
   - Remove any other inbound rule. Outbound: leave open.
5. **Servers → Add server**:
   - Location: any in the EU (Falkenstein, Nuremberg or Helsinki)
   - Image: **Ubuntu 24.04**
   - Type: shared vCPU, x86, the smallest with **4 GB RAM** (about €4–5/month)
   - Networking: public IPv4 **on**, IPv6 on
   - SSH key: the one you added; Firewall: `council`
   - **Backups: off.** Death means no backup copy, so the server must not keep one.
   - Name: `council`. Create.
6. Note the server's IPv4 address. If its card pays the rent, also note the monthly price and the next invoice date under **Billing**: you'll enter both in the setup wizard ("Its server").

If your home IP changes later, edit the firewall's port 22 rule, or open the server's console in Hetzner's web page.

## 2b. The server on Azure (student credits)

Use this instead of step 2 if you have Azure for Students.

1. Sign in at <https://portal.azure.com> with your school account. Check you see the **Azure for Students** subscription (Home → Subscriptions).
2. **Virtual machines → Create → Azure virtual machine**. On **Basics**:
   - Subscription: **Azure for Students** · Resource group: **Create new** → `survival-council`
   - Virtual machine name: `council` · Region: one in Europe (if it says your subscription isn't allowed there, try another: North Europe, Sweden Central, Germany West Central…)
   - Availability options: **No infrastructure redundancy required**
   - Image: **Ubuntu Server 24.04 LTS – x64 Gen2**
   - Size: **B2s** (2 vCPUs, 4 GiB memory), or another size with **4 GiB**. Look at the monthly cost Azure shows next to it: your credit pays it.
   - Authentication: **SSH public key** · Username: `azureuser` · SSH public key source: **Use existing public key** → paste the key from step 1
   - Inbound ports: **Allow selected ports** → **SSH (22)** and **HTTPS (443)**
3. **Disks**: OS disk type **Standard SSD**.
4. **Management**: **Auto-shutdown off** (it must run 24/7). **Backup off** (death means no backup copy).
5. **Review + create → Create**. Wait for "Your deployment is complete" → **Go to resource**.
6. Lock SSH to your IP: **Networking → Network settings** → the rule for port 22 → Source: **My IP address** → Save.
7. A free web address (if you don't want to use your own domain): on the VM's **Overview**, next to **DNS name**, click **Not configured** → DNS name label, for example `survival-council-lea` → Save. Your dashboard address becomes `survival-council-lea.<region>.cloudapp.azure.com`.
8. Note the **Public IP address**.

Then install with the Azure user name:

```bash
deploy/push.sh <public-ip> <dashboard-address> azureuser
```

and on the server use `sudo council …` (for example `sudo council check`), logging in with `ssh azureuser@<public-ip>`.

**Credit:** a 4 GiB VM uses a noticeable part of the $100 student credit every month; check **Cost Management** from time to time. When the credit runs out, Azure stops the VM: the AI stops with it (its positions stay open on Alpaca). The free 1 GiB sizes are too small for its browser.

## 3. Your domain

Skip this if you use Azure's free address from step 2b.7.

At your domain registrar, add a DNS record:

| Type | Name | Value |
|---|---|---|
| A | `council` (or the subdomain you chose) | the server's IPv4 |

Wait a few minutes. Check from your Mac: `dig +short council.yourdomain.com` must print the server's IP.

## 4. Install

From the project folder on your Mac (replace the IP and domain):

```bash
deploy/push.sh 203.0.113.10 council.yourdomain.com
```

This copies the code and runs the installer on the server (5–10 minutes the first time). It installs:

- two Linux users: `council` (the game, the watchdog, the vault) and `council-ai` (the AI's own code, in the bubblewrap sandbox)
- the services `council` (watchdog, brain, scripts, gateway), `council-dashboard`, `council-netfilter` (the AI's code can reach the internet but not the dashboard, the server's metadata service or private networks) and Caddy (HTTPS)
- automatic security updates, a firewall that only lets in 22 and 443, and SSH with keys only
- everything starts again by itself after a reboot

Then set the dashboard password and check the server:

```bash
ssh root@203.0.113.10
```

```bash
council dashboard-password
```

```bash
council check
```

Every line should say `ok`, except the vault lines (`--`), which fill in during the wizard.

## 5. The setup wizard, in paper mode

Open `https://council.yourdomain.com`, log in, and follow the four steps: accounts, its card, keys & email (use the **paper** Alpaca keys), and who pays the server. When every check is ✓, press **Wake it up: start Genesis**.

In paper mode Survival starts as soon as it chooses its split. Watch it.

To test death safely in paper mode, on the server:

```bash
council paper-force-networth 10
```

The watchdog sees Net Worth $10 at its next check (within 15 seconds) and runs the death procedure.

## 6. Going live

When you're ready for real money:

```bash
council use live
```

The service restarts in live mode, fresh: new Genesis, empty memory, new ledger. The dashboard shows the setup wizard again. Enter the **live** Alpaca keys (trading on; withdrawals only exist in Alpaca's own app). The card, xAI, Tavily and email stay saved. Start Genesis. When it has chosen its split, the dashboard shows you exactly what to transfer to Alpaca and the new card limit. Survival starts when the deposit arrives.

## Day to day

Nothing. The only things you might do, on the server:

| | |
|---|---|
| `council pause` / `council resume` | freeze or unfreeze the AI and its scripts (not death; the clock keeps running) |
| `council status` | one line: Net Worth, floor, distance from death |
| `council check` | re-check permissions, sandbox and services |
| `journalctl -u council -f` | the protected side's log |

**Updating the code** later: `deploy/push.sh <ip> <domain>` again. A wake-up in progress is interrupted and resumes as "interrupted" after the restart.

**If it dies**, the `council` service stops for good (it refuses to start). The dashboard stays up with the death screen and its full history.

**If its card pays the rent and the Wallet can't cover it**, the dashboard warns you 3 days before. If the rent fails, the provider stops the server and its positions stay open on Alpaca; you can close them in Alpaca's app.
