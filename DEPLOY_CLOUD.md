# ZeroBot — Cloud Deployment Guide (Hetzner Mumbai / AWS ap-south-1)

## Recommended server: Hetzner CX21 Mumbai (~₹700/month)
- 2 vCPU | 4 GB RAM | 40 GB SSD | Mumbai DC → 2–5ms to NSE

---

## Step 1: Create server

**Hetzner (cheapest, recommended):**
1. hetzner.com → Cloud → Create Server
2. Location: `in-bom1` (Mumbai) ← IMPORTANT
3. Image: Ubuntu 22.04 LTS
4. Type: CX21 (₹700/mo)
5. Add your SSH public key

**AWS (alternative, more tooling):**
- Region: `ap-south-1` (Mumbai)
- Instance: `t3.medium` (~₹2,500/mo)
- AMI: Ubuntu 22.04 LTS

---

## Step 2: First login and setup

```bash
ssh root@YOUR_SERVER_IP

# Create non-root user
adduser trader
usermod -aG sudo trader
su - trader

# Update and install Python
sudo apt update && sudo apt upgrade -y
sudo apt install python3.11 python3.11-venv python3-pip git nginx -y

# Set IST timezone (CRITICAL)
sudo timedatectl set-timezone Asia/Kolkata
timedatectl   # verify shows IST
```

---

## Step 3: Upload ZeroBot

```bash
# From your local machine (Windows: use WinSCP or Git Bash):
scp -r ZB_FINAL/ trader@YOUR_SERVER_IP:~/

# On server:
cd ~/ZB_FINAL
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Step 4: Configure credentials

```bash
nano config/.env
```

Fill in every `YOUR_...` placeholder:
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (get from @BotFather)
- `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_MPIN`, `ANGEL_TOTP_SECRET`
- `SHOONYA_USER`, `SHOONYA_PASSWORD`, `SHOONYA_TOTP_SECRET` etc (if using Shoonya)
- `DASHBOARD_PASS=your_strong_password` (protects dashboard from internet)

```bash
chmod 600 config/.env   # only your user can read it
```

---

## Step 5: Test paper mode (always do this first)

```bash
# Verify market hours gate bypass for testing
export ZEROBOT_FORCE_MARKET_OPEN=1
python3 main.py
# Watch for "ALL N CHECKS PASSED" — fix anything that fails
# Ctrl+C to stop
unset ZEROBOT_FORCE_MARKET_OPEN
```

---

## Step 6: Install systemd service (auto-start on boot)

```bash
# Edit the service file — update paths if yours differ
nano deploy/zerobot.service

# Install
sudo cp deploy/zerobot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable zerobot
sudo systemctl start zerobot

# Check it's running
sudo systemctl status zerobot

# Watch live logs
sudo journalctl -u zerobot -f
```

---

## Step 7: Setup nginx (secure dashboard access)

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/zerobot
sudo ln -s /etc/nginx/sites-available/zerobot /etc/nginx/sites-enabled/
sudo nginx -t   # test config
sudo systemctl reload nginx

# Dashboard now accessible at: http://YOUR_SERVER_IP/
# (with DASHBOARD_PASS set, it will prompt for login)
```

---

## Step 8: Switch to live trading

Only after running paper mode for 3+ weeks without issues:

```bash
# Edit settings.yaml
nano config/settings.yaml
# Change:
#   broker:
#     name: "a_live"   # or "dual" for Angel data + Shoonya execution
#   bot:
#     mode: "live"

sudo systemctl restart zerobot
```

---

## Useful commands (daily operations)

```bash
# View live logs
sudo journalctl -u zerobot -f

# Restart after config change
sudo systemctl restart zerobot

# Emergency stop
sudo systemctl stop zerobot
# OR send /halt via Telegram bot

# View today's logs only
sudo journalctl -u zerobot --since today

# Check disk space (logs can grow)
df -h
du -sh ~/ZB_FINAL/logs/

# Update code and restart
cd ~/ZB_FINAL
git pull   # if using git
sudo systemctl restart zerobot
```

---

## Monitoring checklist (run daily for first 2 weeks)

```
Morning (before 9:15 AM IST):
  □ systemctl status zerobot → should show "active (running)"
  □ Check Telegram — startup notification received?
  □ Dashboard http://YOUR_IP → positions/capital correct?

During market hours:
  □ Telegram alerts firing for signals?
  □ /status command responds?
  □ Dashboard shows live prices?

After 3:30 PM IST:
  □ Daily report received on Telegram?
  □ All positions closed (auto square-off at 3:15)?
  □ Check logs/errors/ for any errors
```

---

## Backup (weekly)

```bash
# Backup DB and config (not code — use git for that)
tar -czf ~/zerobot_backup_$(date +%Y%m%d).tar.gz \
    ~/ZB_FINAL/data/zerobot.db \
    ~/ZB_FINAL/config/.env \
    ~/ZB_FINAL/logs/
```
