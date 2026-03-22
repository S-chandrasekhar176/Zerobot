# 🗄️ PostgreSQL Setup Guide (pgAdmin 4)

## Step 1: Open pgAdmin 4
Launch pgAdmin 4 from your Start Menu.

## Step 2: Create Database User
In pgAdmin → Expand "Servers" → Right-click "Login/Group Roles" → Create → Login/Group Role

**General tab:**
- Name: `zerobot_user`

**Definition tab:**
- Password: (choose a strong password — write it down)
- Confirm password: same

**Privileges tab:**
- Can login? → YES
- Create databases? → YES

Click **Save**.

## Step 3: Create Database
Right-click "Databases" → Create → Database

- **Database:** `zerobot`
- **Owner:** `zerobot_user`

Click **Save**.

## Step 4: Update .env File
Open `config/.env` and fill in:
```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=zerobot
DB_USER=zerobot_user
DB_PASSWORD=YOUR_PASSWORD_FROM_STEP_2
```

## Step 5: Initialize Tables
Run this once to create all tables:
```bash
python -c "
import sys; sys.path.insert(0, '.')
from database.models import init_db
init_db()
print('Tables created!')
"
```

## Step 6: Verify in pgAdmin
Expand: `zerobot` → `Schemas` → `public` → `Tables`

You should see:
- trades
- positions  
- signals
- ohlcv_1min
- ohlcv_daily
- model_runs
- risk_events
- audit_log
- bot_state

---

## If pgAdmin shows "Connection refused"
PostgreSQL service might not be running:
1. Press Win+R → type `services.msc`
2. Find "postgresql-x64-XX"
3. Right-click → Start

## Connection String (for reference)
`postgresql://zerobot_user:PASSWORD@localhost:5432/zerobot`
