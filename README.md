# SpectraAssure_Portal_Usage

Python script that leverages the Spectra Assure Portal API to gather all usage data and builds an interactive HTML report of how your organization uses its analysis capacity: per month, per group, per file stream, project, package, and user.

The script fetches the data through the Portal API and produces one self-contained HTML file. You can open it offline, email it, or put it on a shared drive. It doesn't require internet access or any other files.

## Disclaimer of Warranty

This application is provided "as is" and "as available" without any warranties of any kind, either express or implied.

Reversing Labs make no representations or warranties of any kind, including but not limited to:

- The accuracy, completeness, or timeliness of the information submitted or received via this application;
- The functionality, availability, or performance of the application;
- The security, integrity, or confidentiality of submitted files or user data; or
- The fitness of this application for any particular purpose.

Use of this application is at your own risk. By using this application, you acknowledge that any data submitted to third-party services (e.g., ReversingLabs Spectra Analyze) may be subject to their own terms and conditions.

In no event shall the developer be liable for any direct, indirect, incidental, special, exemplary, or consequential damages arising out of or in any way connected with the use or misuse of this application.

## Requirements to run the script 

| What | Details |
|---|---|
| Python | 3.11 or newer |
| Python libraries | `spectra-assure-sdk` 1.1.0 or newer (ReversingLabs' official Python SDK for the Portal API) and `jinja2` (builds the HTML report). Both are installed in Setup step 2 |
| Portal role | **Organization administrator**, required to export audit logs |
| Personal Access Token (PAT) | Portal → *User Settings* → *Personal Access Tokens*. Revoke and generate new token when existing expires. More info how to generate token can be found here: https://docs.secure.software/api/generate-api-token  |

## Installation

1. **Install the libraries:**
   ```
   pip install -r requirements.txt
   ```

2. **Create your config file.**

   Copy `config.example.toml`, name the copy `config.toml`, and fill in the three values:
   ```config.toml
   base_url     = "https://{portalUrl}/api/public/v1"
   organization = "your-organization"
   token        = "your-personal-access-token"
   ```
   - `base_url`: your Portal's public API address, with no slash at the end
   - `organization`: the organization name exactly as shown in the Portal (case-sensitive)
   - `token`: your PAT

## Usage

```
python run_report.py [options]
```

Run time is normally less than 1 minute, most of which is waiting for the Portal to prepare the audit log export file. When it's done, the report opens in your web browser automatically. By default, the report includes activity from the last 90 days.

### Options

| Option | What it does |
|---|---|
| `--help` | List all available options |
| `--config <CONFIG_PATH>` | Specifies config file where to read required Portal parameters |
| `--days <DAYS>` | Number of days covered by the report, e.g. `--days 30` or `--days 365` (min 1, max 365; default 90) |
| `--timeout <TIMEOUT>` | Wait longer for the export (seconds; default 600) |
| `--dry-run` | Show which public APIs would be called; calls nothing |
| `--job-id <JOB_ID>` | Reuse an export that's already prepared and exists in the Portal (skips starting a new one) |
| `--from-file <FILE_PATH>` | Rebuild a run's report from its saved files with **no API calls**, e.g. `--from-file output\Trial_2026-10-26_14-30-05\raw\audit_Trial_20260728_20261026.ndjson.zip`. The rebuilt report replaces the one in that run's folder |
| `--no-open` | Build the report without opening the browser automatically |

## Directories and files created

```
output/
├── run_log.txt                                 ← what the LATEST run printed
├── Trial_2026-10-20_09-15-42/                  ← one folder per run: <org>_<date>_<time>
└── Trial_2026-10-26_14-30-05/
    ├── report_<org>_<from>_<to>.html           ← the report: open or share this .html file
    ├── run_log.txt                             ← what this run printed
    └── raw/                                    ← untouched API answers
        ├── audit_<org>_<from>_<to>.ndjson.zip  (as downloaded)
        ├── audit_<org>_<from>_<to>.ndjson      (unpacked)
        ├── usage_<org>_<date>.json             (response from GET /usage/{organization})
        ├── groups_<org>_<date>.json            (response from GET /list/{organization})
        └── audit_status_last.json              (response from GET /audit-log/{organization}/export/{job_id}/status)
```

**Every run is kept.** Each run creates its own folder, named after the organization and the date and time of the run, so earlier reports and exports are never overwritten or deleted. Folders sort by date, so the newest run is at the bottom. To free up space, delete old run folders yourself. If a run fails, its folder may hold only part of the files; its `run_log.txt` explains what went wrong.

## Using the generated HTML report

1. **Filters at the top:** 

| Filter | What it does |
|---|---|
| Group | User can select to check usage by specific group or all groups combined |
| Upload type | User can choose to check quota usage only on Projects, File stream or both|
| Project | Enabled only when specific group is selected and upload_type = All, or Projects. Allows users to check quota usage in specific Project within the group selected |
| Package | Enabled only when specific Project is selected. Allows users to check quota usage in specific Package witin the Project selected |
| User | Search quota usage by specific user |
| Month | Quick filter to select month user wants to analyze |
| Data range| Select specific period for quota consumption analysis |

- User can type in a box to search. 
- *Clear filters* resets everything.
- Choosing a group narrows the project, package and user lists.

2. **Monthly analysis capacity:** Shows total monthly capacity contracted, Portal + CLI capacity distribution, plus Flex quota if organization contracted it.

3. **Quota used per month:** Shows monthly totals for selected filter and time range against the monthly analysis capacity and Flex lines. Hover a bar for quota usage details.

4. **This month's usage:** Show analysis capacity, download limit and package limit, taken from the usage API, only for current month.

5. **Group usage:** Shows quota usage for selected filters per group, with each group's monthly limit.

6. **Uploads:** Shows one row per upload for selected filters, up to 10 per page. Expand a row to see all event details, including size of the uploaded file in bytes.

### Good to know

- **Sizes:** "GB" here means 1,073,741,824 bytes (1024³), the same unit the Portal uses, so the numbers match the Portal.
- **Dates and times** are shown in US format (MM/DD/YYYY, 12-hour clock), in the time zone of the computer that opens the report.
- **Months** are calendar months (from the 1st to the end of the month) in the viewer's time zone.
- **Audit history:** a normal run exports the last 90 days (change it with `--days`; the API allows at most 365 days per export), and the report only shows what the audit log contains. Audit logs are available only from 06/03/2026 when the feature was released, so if the period starts earlier, months before that show no uploads (the report then notes this under the chart and in the ⓘ popup).

## Troubleshooting

| Message | What to do |
|---|---|
| `token invalid or expired` (401) | Create a new PAT in the Portal and paste it into `config.toml` |
| `Your account needs the Organization administrator role…` (403) | Ask an org admin to grant the role, or run with an admin's token |
| `no access / missing role for this action` (403) | Your token can't read this data. Check the organization name and your role |
| `not found — check base_url / organization…` (404) | Check `base_url` (ends in `/api/public/v1`, no trailing slash) and the organization spelling |
| `too many requests` (429) | The script waits and retries by itself (up to 5 times); if it still fails, try again later |
| Export not ready after 10 minutes | Run again with `--timeout 900`, or use `--job-id <id>` with the ID shown in the log |
| `python` is not recognized | Use `py` instead of `python`, or install Python from python.org (tick *Add to PATH*) |
| Anything else | Open `output\run_log.txt` (latest run) or the `run_log.txt` in that run's folder; it contains the full details (never the token) |


## License

MIT — see [LICENSE](LICENSE).
