# Lidl Leaflet Downloader

A small Python tool that checks Lidl's UK online leaflets page, downloads any new leaflets, and optionally sends notifications via Pushover or a Home Assistant webhook.

## What it does

- Fetches the current Lidl online leaflets for a selected store.
- Downloads the PDF for each leaflet.
- Keeps a local archive with versioned copies.
- Sends a notification when a new or changed leaflet is saved.
- Supports:
  - [Pushover](https://pushover.net/)
  - [Home Assistant](https://www.home-assistant.io/) webhooks

## Requirements

- Python 3.10 or newer
- Windows, Linux, or macOS

## Setup

1. Clone or download the repository.

2. Create a virtual environment and install dependencies:

   ```bash
   python3 -m venv venv
   source venv/bin/activate        # Linux/macOS
   # or
   python -m venv venv
   venv\Scripts\activate           # Windows
   ```

3. Install Python packages:

   ```bash
   pip install -r requirements.txt
   ```

4. Install the Chromium browser for Playwright:

   ```bash
   playwright install chromium
   ```

5. Copy `config.example.json` to `config.json` and fill in your own values:

   ```bash
   cp config.example.json config.json
   ```

## Configuration

See `config.example.json` for the available options.

| Key | Description |
| --- | --- |
| `output_dir` | Folder where leaflets are saved. |
| `headless` | Run the browser without a window (`true` / `false`). |
| `pushover.enabled` | Enable Pushover notifications. |
| `pushover.token` | Your Pushover application token. |
| `pushover.user` | Your Pushover user key. |
| `home_assistant.enabled` | Enable Home Assistant webhook notifications. |
| `home_assistant.webhook_url` | Your Home Assistant webhook URL. |

### Home Assistant webhook

Create a webhook automation in Home Assistant using the YAML in `homeassistant-automation.example.yaml`. Replace the webhook URL in `config.json` with the URL shown in the automation editor.

The webhook receives one of two payloads:

**New leaflet:**

```json
{
  "type": "new_leaflet",
  "filename": "01-10-07-10-lidl-weekly.pdf",
  "local_path": "/path/to/leaflets/...",
  "viewer_url": "https://www.lidl.co.uk/l/en/online-leaflets/.../view/flyer/page/1"
}
```

**Error:**

```json
{
  "type": "error",
  "message": "..."
}
```

## Usage

Windows:

```bash
venv\Scripts\activate   # Windows
python download_lidl.py
```

On Linux or macOS:

```bash
source venv/bin/activate # Linux/macOS
python3 download_lidl.py
```

## Automation

You can schedule the script with Windows Task Scheduler, cron, or any other scheduler.

Example cron entry running every Tuesday morning:

```cron
0 9 * * 2 cd /path/to/lidl-leaflets && venv/bin/python3 download_lidl.py
```

## Files

| File | Description |
| --- | --- |
| `download_lidl.py` | Main script. |
| `config.json` | Your local configuration (not committed). |
| `config.example.json` | Example configuration with placeholder values. |
| `requirements.txt` | Python dependencies. |
| `homeassistant-automation.example.yaml` | Example Home Assistant automation. |



### `config.example.json`

```json
{
  "output_dir": "leaflets",
  "headless": true,
  "pushover": {
    "enabled": false,
    "token": "",
    "user": ""
  },
  "home_assistant": {
    "enabled": false,
    "webhook_url": ""
  }
}
```

### `homeassistant-automation.example.yaml`

```yaml
alias: Lidl leaflet notification
description: Notify when a new Lidl leaflet is downloaded
trigger:
  - platform: webhook
    webhook_id: lidl_leaflet
    allowed_methods:
      - POST
    local_only: false
condition: []
action:
  - choose:
      - conditions:
          - condition: template
            value_template: "{{ trigger.json.type == 'new_leaflet' }}"
        sequence:
          - action: notify.mobile_app_your_phone
            data:
              title: "New Lidl leaflet"
              message: "{{ trigger.json.filename }}"
              data:
                channel: LidlLeaflets
                tag: "{{ trigger.json.filename }}"
                clickAction: "{{ trigger.json.viewer_url }}"
                actions:
                  - action: URI
                    title: "Open flyer"
                    uri: "{{ trigger.json.viewer_url }}"
                notification_icon: mdi:basket
                color: red
      - conditions:
          - condition: template
            value_template: "{{ trigger.json.type == 'error' }}"
        sequence:
          - action: notify.mobile_app_your_phone
            data:
              title: "Lidl leaflet download failed"
              message: "{{ trigger.json.message[:250] }}"
              data:
                channel: LidlLeaflets
                tag: lidl_leaflet_error
                notification_icon: mdi:alert
                color: red
mode: parallel
max: 10
```

## Note

Replace `notify.mobile_app_your_phone` with your actual Home Assistant mobile app notification target.