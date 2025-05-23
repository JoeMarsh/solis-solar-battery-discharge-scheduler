# Solis Solar Battery Discharge Scheduler

This project provides a Python script to dynamically manage the discharge schedule of a Solis solar battery system. It interacts with the Solis Cloud API to fetch battery state of charge (SOC) and set inverter discharge parameters. The script can be run manually or automatically via GitHub Actions, with notifications sent to a Discord webhook.

## Features

- Interacts with the Solis Cloud API to fetch battery data and set inverter parameters.
- Dynamically calculates and sets battery discharge amperage and time based on current State of Charge (SOC).
- Sends notifications to a configured Discord webhook about its operations (e.g., current SOC, actions taken, errors).
- Includes GitHub Actions workflow for scheduled execution (e.g., daily adjustments of discharge schedule).
- Allows manual execution with a command-line argument to specify discharge duration.

## Requirements

- Python 3.x
- `requests` Python library
- Solis Cloud API Key
- Solis Cloud API Secret
- Solis Inverter Serial Number (SN)
- Discord Webhook URL

## Setup

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/your-username/solis-solar-battery-discharge-scheduler.git
    cd solis-solar-battery-discharge-scheduler
    ```
    *(Note: Replace `your-username` with the actual repository path if different)*
2.  **Install dependencies:**
    ```bash
    pip install requests
    ```
3.  **Set up environment variables:**
    The script requires the following environment variables to be set. You can set these in your operating system, in a `.env` file (if you modify the script to load it, e.g., using `python-dotenv`), or directly in the GitHub Actions secrets if using the automated workflow.
    - `API_KEY`: Your Solis Cloud API Key.
    - `API_SECRET`: Your Solis Cloud API Secret.
    - `INVERTER_SN`: Your Solis Inverter Serial Number.
    - `DISCORD_WEBHOOK_URL`: Your Discord Webhook URL for notifications.

## Usage

### Manual Execution
You can run the script manually from your terminal. This is useful for testing or one-off adjustments.
```bash
python script.py --hours X
```
- Replace `X` with the desired discharge duration in hours. For example, to discharge for 2 hours:
  ```bash
  python script.py --hours 2
  ```
- If the `--hours` argument is omitted, it defaults to 1 hour.

### Scheduled Execution with GitHub Actions
The project includes a GitHub Actions workflow defined in `.github/workflows/schedule.yml` to automate the script execution.

1.  **Configure Secrets:** For the scheduled execution to work, you need to add the following secrets to your GitHub repository (Settings > Secrets and variables > Actions > New repository secret):
    - `API_KEY`: Your Solis Cloud API Key.
    - `API_SECRET`: Your Solis Cloud API Secret.
    - `INVERTER_SN`: Your Solis Inverter Serial Number.
    - `DISCORD_WEBHOOK_URL`: Your Discord Webhook URL.

2.  **Workflow Details:**
    - The workflow is currently configured to run at 00:00 UTC daily (passing `--hours 2`) and 01:00 UTC daily (passing `--hours 1`). You can adjust the cron schedule in the `schedule.yml` file to fit your needs.
    - It also allows for manual triggering from the GitHub Actions tab. When triggered manually without a specific schedule context, it runs `python script.py` (which defaults to `--hours 1`).
    ```yaml
    # Excerpt from .github/workflows/schedule.yml
    # on:
    #   schedule:
    #     - cron: '0 0 * * *'  # Runs at 00:00 UTC daily
    #     - cron: '0 1 * * *'  # Runs at 01:00 UTC daily
    #   workflow_dispatch:  # Allows manual triggering
    ```
    *(Note: The workflow in the template is commented out. You will need to uncomment it in your repository for it to run.)*

## Configuration

The primary way to configure the script is through environment variables, as detailed in the "Setup" section. However, there are also some constants within `script.py` that you might want to be aware of or adjust if your setup differs significantly:

-   **API Endpoint:**
    -   `API_URL = "https://www.soliscloud.com:13333"`: The base URL for the Solis Cloud API. This is unlikely to change but is defined at the top of the script.

-   **Battery Specifications:**
    These constants are used to calculate the appropriate discharge current. Ensure they match your battery system's specifications for optimal performance and safety.
    -   `BATTERY_NOMINAL_CAPACITY = 400`: Ah (e.g., 4 x 100Ah batteries).
    -   `BATTERY_OPERATING_VOLTAGE_MIN = 44.8`: V
    -   `BATTERY_OPERATING_VOLTAGE_MAX = 57.6`: V
    -   `BATTERY_MAX_DISCHARGE_CURRENT = 100`: A (Maximum safe continuous discharge current for your battery).

-   **Discharge Logic:**
    -   `soc_to_discharge = soc - 20`: In `calculate_discharge_current`, the script aims to discharge the battery down to 20% SOC. You can change the `20` to a different target minimum SOC.
    -   `charge_time_range = "02:05-05:55"`: In `set_inverter_parameters`, this is a fixed time range for when the inverter is allowed to charge the battery from the grid. Adjust this to your tariff or preferences.
    -   `discharge_end_hour = 2`: In `set_inverter_parameters`, this defines when the discharge period should end (02:00 in this case). The script calculates the start time based on this and the `--hours` parameter.

-   **GitHub Actions Schedule:**
    -   The schedule for automated runs is defined in `.github/workflows/schedule.yml` using cron expressions. You can modify these to change the frequency and timing of the scheduled tasks.

## Contributing

Contributions are welcome! If you have suggestions for improvements, new features, or bug fixes, please feel free to:
1.  Fork the repository.
2.  Create a new branch for your changes.
3.  Make your changes and commit them with clear messages.
4.  Push your branch and open a pull request.

## License

This project is licensed under the MIT License. See the LICENSE file for details.

*(Note: You may need to create a `LICENSE` file containing the full text of the MIT License if one doesn't already exist.)*
