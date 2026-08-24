# Zattoo EPG Grabber

A Python program that downloads EPG data from Zattoo and saves it as an XMLTV file. This is a Python implementation of the [easyEPG project](https://github.com/sunsettrack4/easyepg).

This maintained fork focuses on reliable Docker and TVHeadend operation. Detailed
programme metadata is stored in a persistent SQLite cache, so programme images can
remain enabled without downloading the same details on every update. A strict
channel filter prevents unnecessary requests for channels that are not used.

## Features

- **Login Authentication**: Uses the same API interface as the original program
- **Multiple Countries**: Supports Germany (DE) and Switzerland (CH)
- **Customizable Time Periods**: 1-14 days of EPG data
- **XMLTV Format**: Compatible output format for EPG viewers
- **Detailed Program Information**: Optionally retrieve detailed program information
- **Console Interface**: Simple command-line operation
- **Programme Image Cache**: Persistent SQLite cache for detailed metadata
- **Channel Filter**: Exact allow-list by Zattoo channel ID or display name
- **Docker Support**: Scheduled generation plus a small HTTP endpoint for XMLTV
- **Atomic Updates**: Readers never see a partially written XMLTV file

## Docker quick start

1. Create local configuration files (they are ignored by Git):

```bash
cp .env.example .env
cp channels.example.txt channels.txt
cp compose.example.yml compose.yml
```

2. Put the Zattoo account in `.env` and list the desired exact channel names in
   `channels.txt`. Never commit `.env` or `config.json`.

3. Build and start:

```bash
docker compose up -d --build
```

The first run fetches the selected channel details and therefore takes longer.
Later runs reuse `/data/program-details.sqlite3`. The guide is regenerated every
12 hours and is available at:

```text
http://SERVER-IP:8080/guide.xml
```

Point TVHeadend's XMLTV URL grabber at that URL. Test this fork on a separate
port/output first; do not replace a working production EPG until channel mapping
and programme images have been verified.

## Experimental DASH replay proxy

Zattoo live DASH manifests can expose a server-side timeshift window. The optional
replay proxy converts a selected part of that dynamic window into a static MPD,
without continuously recording every channel. Enable it only on a trusted network:

```env
TELERISING_BASE_URL=http://YOUR-TELERISING-HOST:5000
```

Then request a channel with an offset in seconds (7200 means two hours):

```text
http://SERVER-IP:8090/replay/CHANNEL.mpd?offset=7200
```

The proxy does not store credentials, video segments, or signed stream URLs. It
fetches a fresh upstream manifest for each request. This endpoint is experimental;
validate one channel with FFmpeg before adding it to TVHeadend or Jellyfin.

## Requirements

- Python 3.9 or higher (Python 3.12 is used by the Docker image)
- Zattoo account (Germany or Switzerland)
- Internet connection

## Installation

1. Clone repository or download files:
```bash
git clone <repository-url>
cd ZattooEPG
```

2. Create virtual environment (recommended):
```bash
python -m venv venv
source venv/bin/activate  # Linux/macOS
# or
venv\Scripts\activate.bat  # Windows
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

## Configuration

### Configuration File (Recommended)

1. Create a `config.json` file based on the example:
```bash
cp config.example.json config.json
```

2. Edit the `config.json` and enter your Zattoo credentials:
```json
{
    "email": "your-email@example.com",
    "password": "your-password"
}
```

Alternatively set `ZATTOO_EMAIL` and `ZATTOO_PASSWORD`. Environment variables
take precedence over `config.json`.

## Usage

### Basic Usage (with configuration file)

```bash
python zattoo_epg.py
```

The program loads credentials from the `config.json` file and downloads 7 days of EPG data for Germany.

# Use interactive login instead of configuration file
```bash
python zattoo_epg.py --interactive
```

# Send EPG data directly to TVHeadend
```bash
python zattoo_epg.py --tvheadend-only
```

# Custom TVHeadend socket path
```bash
python zattoo_epg.py --tvheadend --tvheadend-socket /path/to/tvheadend/xmltv.sock
```

### Advanced Options

```bash
# Swiss EPG for 3 days
python zattoo_epg.py --country CH --days 3

# Without detailed information (much faster - recommended)
python zattoo_epg.py --no-details

# Custom output file
python zattoo_epg.py --output my_epg.xml

# Maximum 14 days
python zattoo_epg.py --days 14

# Custom configuration file
python zattoo_epg.py --config my_config.json

# Only selected channels, with a persistent detail/image cache
python zattoo_epg.py --country CH --days 7 \
  --channel-filter channels.txt \
  --cache cache/program-details.sqlite3

# Interactive login (without configuration file)
python zattoo_epg.py --interactive
```

### Complete Options

```bash
python zattoo_epg.py --help
```

Available parameters:
- `--country/-c`: Country (DE or CH, default: DE)
- `--days/-d`: Number of days (1-14, default: 7)
- `--output/-o`: Output file (default: zattoo_epg.xml)
- `--no-details`: Skip detailed information (faster)
- `--config`: Configuration file with email and password (default: config.json)
- `--interactive`: Use interactive login instead of configuration file
- `--channel-filter`: Exact channel IDs or names, one per line
- `--cache`: SQLite file for detailed programme metadata
- `--cache-ttl-days`: Lifetime of cached details (default: 30 days)
- `--tvheadend`: Send EPG data to TVHeadend after generation
- `--tvheadend-socket`: Path to TVHeadend XMLTV socket (default: /var/lib/tvheadend/epggrab/xmltv.sock)
- `--tvheadend-only`: Send EPG data directly to TVHeadend without saving to file

## Output Format

The program creates an XMLTV file with the following information:

### Channels
- Channel ID
- Channel name
- Channel logo (if available)

### Programs
- **Basic Information**: Title, start/end time, description
- **Additional Information**: Subtitle, production year, country
- **Categories**: Genres and program categories
- **Credits**: Directors and actors
- **Episode Information**: Season and episode numbers
- **Age Rating**: FSK rating
- **Images**: Program posters/images

## Example Output

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE tv SYSTEM "xmltv.dtd">
<tv source-info-url="https://zattoo.com/" source-data-url="https://zattoo.com/" generator-info-name="Zattoo EPG Grabber Python">
  <channel id="cid://tx.zattoo.com/119">
    <display-name lang="de">Das Erste HD</display-name>
    <icon src="https://images.zattic.com/cms/..." />
  </channel>
  
  <programme start="20250909180000 +0000" stop="20250909184500 +0000" channel="cid://tx.zattoo.com/119">
    <title lang="de">Tagesschau</title>
    <desc lang="de">Nachrichten des Tages</desc>
    <category lang="de">Nachrichten</category>
    <icon src="https://images.zattic.com/cms/.../original.jpg" />
  </programme>
</tv>
```

## Performance Optimization

### Recommended Settings for Best Performance

**For maximum speed (recommended):**
```bash
python zattoo_epg.py --no-details
```
- Only loads basic EPG data (title, time, description)
- **Very fast**: ~1-2 seconds per day
- Sufficient for most EPG viewers

**For detailed information (slow):**
```bash
python zattoo_epg.py
```
- Loads additional details (actors, directors, episode information)
- **Slow**: 20-30 minutes per day
- May be interrupted by rate limiting

### Performance Issues

Loading detailed information can be slow on the first run. This fork sends batched
requests and caches results by programme ID. Keep the cache volume persistent and
use a channel filter to minimize requests. A normal refresh then downloads only
previously unseen programmes.

Use `--no-details` only when programme images and extended metadata are not needed.

## TVHeadend Integration

### Automatic Sending to TVHeadend

The program can send generated EPG data directly to TVHeadend:

```bash
# Create EPG and send to TVHeadend
python zattoo_epg.py --no-details --tvheadend

# Send directly to TVHeadend without creating file
python zattoo_epg.py --no-details --tvheadend-only
```

### TVHeadend Configuration

1. **Enable XMLTV Grabber**:
   - In TVHeadend: Configuration → Channel/EPG → EPG Grabber
   - Enable "XMLTV"
   - Socket path: `/var/lib/tvheadend/epggrab/xmltv.sock`

2. **Check permissions**:
   ```bash
   # Check socket path
   ls -la /var/lib/tvheadend/epggrab/xmltv.sock
   
   # Add user to tvheadend group (if necessary)
   sudo usermod -a -G tvheadend $USER
   ```

3. **Automation with Cron**:
   ```bash
   # Update EPG daily at 6:00 AM
   0 6 * * * /path/to/python /path/to/zattoo_epg.py --no-details --tvheadend-only
   ```

### TVHeadend Troubleshooting

- **Socket not found**: TVHeadend is not running or XMLTV grabber is not enabled
- **Permission denied**: User does not have permission to access the socket
- **Connection failed**: TVHeadend XMLTV grabber is not configured

## Troubleshooting

### Common Issues

1. **Login failed**
   - Check your Zattoo credentials
   - Ensure your account is valid for the selected country

2. **Network errors**
   - Check your internet connection
   - Zattoo service may be temporarily unavailable

3. **Empty EPG file**
   - Possibly no program data available for the selected time period
   - Try a shorter time period

### Debug Information

The program outputs detailed progress information:
- Session token status
- Login status
- Number of loaded channels
- Download progress
- Number of processed programs

## License

This program is licensed under the GNU General Public License v3.0, just like the original easyEPG project.

## Contributors

Based on the original [easyEPG project](https://github.com/sunsettrack4/easyepg) by:
- [Jan-Luca Neumann (sunsettrack4)](https://github.com/sunsettrack4)

Python implementation developed as an equivalent alternative to the original Bash/Perl script.
