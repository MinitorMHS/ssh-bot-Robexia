# 🚀 ssh-bot-Robexia

A powerful Telegram SSH/SFTP Bot that lets users connect to their servers directly through Telegram.
Perfect for situations where internet access is limited, but Telegram is still available.

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.fa.md">فارسی</a>
</p>

<p align="center">
  <a href="#features">Features</a> ·
  <a href="#quick-start">Quick Install</a> ·
  <a href="#bot-commands">Bot Commands</a>
</p>

---

## ✨ Features

* ⚡ Fast SSH connection using IP, username, and password
* 🔍 Automatic detection of default SSH port `22`
* 🛠 Custom port support
* 💻 Live terminal inside Telegram
* 📤 Send commands and receive instant output
* 📁 Save and manage server list (**My Hosts**)
* 🔄 Quick reconnect to saved servers
* ⏸ Temporary exit from terminal using `wait`
* ❌ Fully close terminal using `close`
* 💤 Auto session close after inactivity
* 📂 Built-in SFTP for file management
* 📤 File upload support (up to 20MB)
* 🎛 Clean and simple control panel
* ⌨ Smart shortcut buttons
* 📝 Dynamic buttons for nano / vim sessions

---

## 📦 Requirements

* Ubuntu 22.04

---

## ⚙️ Quick Install

Run this command on your server:

```bash
bash <(curl -sSL https://raw.githubusercontent.com/Hajiyor/ssh-bot-Robexia/main/install.sh)
```

### Installer will automatically:

* Install all dependencies
* Ask for your bot token
* Ask for admin numeric ID
* Configure the bot
* Start the bot automatically

---

## 🤖 Bot Commands

| Command     | Description                             |
| ----------- | --------------------------------------- |
| `/start`    | Return to main panel                    |
| `/fast_ssh` | Quick connection (without saving)       |
| `/my_hosts` | Saved servers (up to 5 hosts)           |
| `/close`    | Close current session                   |
| `/wait`     | Background current session (15 minutes) |

---

## 💻 Inside Terminal

* Any text you send → executes as SSH command
* Buttons provide useful shortcuts
* When nano / vim is open, buttons change automatically

---

## 📂 SFTP Mode

* Select **SFTP** during quick connection
* Send files inside session → uploaded automatically
* Maximum upload size: **20MB**

---

## ⏰ Session Timeout

Inactive sessions are automatically closed after **5 minutes**.

---

## 💖 Support / Donate

If this project helped you, you can support development with a donation.

* **TON:** `UQAhWF7xgZuODgzjBZKahSpi_nDisNgjQp-G14fxfCILRaZN`
* **TRON (TRC20):** `TPWe5ZpnWTzuNZ9EKCCP1MiJeeWzjZsRAM`

---

## 📢 Official Telegram Channel

For updates, news, and future releases:
https://t.me/Robexia
