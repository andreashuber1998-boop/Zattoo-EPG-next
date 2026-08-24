#!/usr/bin/env python3
"""
Zattoo EPG Grabber - Python Implementation
A Python program that downloads EPG data from Zattoo and saves it as XMLTV format.

Usage: python zattoo_epg.py

This program mimics the functionality of the original easyEPG Zattoo grabber.
"""

import requests
import json
import sys
import os
import getpass
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import time
import re
from typing import Dict, List, Optional, Any, Union
import argparse
import socket
import subprocess
import sqlite3
import html
from pathlib import Path
from zoneinfo import ZoneInfo


class ZattooEPG:
    """Main class for Zattoo EPG downloading and processing."""
    
    def __init__(
        self,
        country: str = "DE",
        channel_filter: Optional[List[str]] = None,
        cache_path: Optional[str] = None,
        cache_ttl_days: int = 30,
    ):
        """Initialize the Zattoo EPG grabber.
        
        Args:
            country: Country code (DE or CH)
        """
        self.country = country.upper()
        self.language = "de" if country.upper() == "DE" else "de"
        self.timezone = ZoneInfo("Europe/Berlin" if self.country == "DE" else "Europe/Zurich")
        self.session = requests.Session()
        self.session_token = None
        self.power_guide_hash = None
        self.channels = {}
        self.epg_data = []
        self.channel_filter = {
            self._normalize_channel_name(item)
            for item in (channel_filter or [])
            if item.strip()
        }
        self.cache_path = cache_path
        self.cache_ttl_seconds = cache_ttl_days * 86400
        if self.cache_path:
            self._init_cache()
        
        # Set User-Agent
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:75.0) Gecko/20100101 Firefox/75.0',
            'Accept': 'application/json',
            'Accept-Language': 'de,en-US;q=0.7,en;q=0.3',
        })
        
        print(f"+++ COUNTRY: {self.country} +++\n")

    @staticmethod
    def _normalize_channel_name(value: str) -> str:
        """Normalize a channel identifier for exact, case-insensitive matching."""
        return " ".join(value.casefold().split())

    def _channel_is_selected(self, cid: str, title: str) -> bool:
        if not self.channel_filter:
            return True
        return bool(
            {
                self._normalize_channel_name(cid),
                self._normalize_channel_name(title),
            }
            & self.channel_filter
        )

    def _init_cache(self) -> None:
        cache_file = Path(self.cache_path)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(cache_file) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS program_details (
                    program_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )

    def _load_cached_details(self, program_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        if not self.cache_path or not program_ids:
            return {}
        minimum_timestamp = int(time.time()) - self.cache_ttl_seconds
        rows = []
        with sqlite3.connect(self.cache_path) as connection:
            # Stay below SQLite's variable limit even for large guides.
            for offset in range(0, len(program_ids), 900):
                chunk = program_ids[offset:offset + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    connection.execute(
                        f"""
                        SELECT program_id, payload
                        FROM program_details
                        WHERE updated_at >= ? AND program_id IN ({placeholders})
                        """,
                        [minimum_timestamp, *chunk],
                    ).fetchall()
                )
        result = {}
        for program_id, payload in rows:
            try:
                result[str(program_id)] = json.loads(payload)
            except json.JSONDecodeError:
                continue
        return result

    def _store_cached_details(self, details: Dict[str, Dict[str, Any]]) -> None:
        if not self.cache_path or not details:
            return
        updated_at = int(time.time())
        rows = [
            (str(program_id), json.dumps(payload, ensure_ascii=False), updated_at)
            for program_id, payload in details.items()
        ]
        with sqlite3.connect(self.cache_path) as connection:
            connection.executemany(
                """
                INSERT INTO program_details(program_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(program_id) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
    
    def get_session_token(self) -> bool:
        """Get initial session token from Zattoo."""
        try:
            print("Loading session token...", end="", flush=True)
            
            # Get app token
            response = self.session.get("https://zattoo.com/token.json")
            if response.status_code != 200:
                print(" FAILED!")
                return False
            
            token_data = response.json()
            app_token = token_data.get('session_token')
            
            if not app_token:
                print(" FAILED!")
                return False
            
            # Initialize session
            data = {
                'client_app_token': app_token,
                'uuid': 'd7512e98-38a0-4f01-b820-5a5cf98141fe',
                'lang': 'en',
                'format': 'json'
            }
            
            response = self.session.post("https://zattoo.com/zapi/session/hello", data=data)
            if response.status_code != 200:
                print(" FAILED!")
                return False
            
            # Extract session ID from cookies
            session_cookie = None
            for cookie in self.session.cookies:
                if cookie.name == 'beaker.session.id':
                    session_cookie = f"beaker.session.id={cookie.value}"
                    break
            
            if not session_cookie:
                print(" FAILED!")
                return False
            
            # Update session headers
            self.session.headers.update({'Cookie': session_cookie})
            print(" OK!")
            return True
            
        except Exception as e:
            print(f" FAILED! Error: {e}")
            return False
    
    def login(self, username: str, password: str) -> bool:
        """Login to Zattoo with user credentials.
        
        Args:
            username: Zattoo username/email
            password: Zattoo password
            
        Returns:
            True if login successful, False otherwise
        """
        try:
            print("Login to Zattoo webservice...", end="", flush=True)
            
            data = {
                'login': username,
                'password': password,
            }
            
            response = self.session.post("https://zattoo.com/zapi/v2/account/login", data=data)
            
            if response.status_code != 200:
                print(" FAILED!")
                return False
            
            result = response.json()
            
            if not result.get('success'):
                print(" FAILED!")
                return False
            
            # Check country
            service_region = result.get('session', {}).get('service_region_country', '')
            if service_region != self.country:
                print(f" WRONG COUNTRY! Expected {self.country}, got {service_region}")
                return False
            
            # Extract power guide hash
            self.power_guide_hash = result.get('session', {}).get('power_guide_hash')
            
            if not self.power_guide_hash:
                print(" FAILED! No power guide hash received.")
                return False
            
            # Update session cookie
            for cookie in self.session.cookies:
                if cookie.name == 'beaker.session.id':
                    session_cookie = f"beaker.session.id={cookie.value}"
                    self.session.headers.update({'Cookie': session_cookie})
                    break
            
            print(" OK!")
            return True
            
        except Exception as e:
            print(f" FAILED! Error: {e}")
            return False
    
    def get_channels(self) -> bool:
        """Get channel list from Zattoo API."""
        try:
            print("Loading channel list...", end="", flush=True)
            
            url = f"https://zattoo.com/zapi/v2/cached/channels/{self.power_guide_hash}?details=False"
            response = self.session.get(url)
            
            if response.status_code != 200:
                print(f" FAILED! HTTP {response.status_code}")
                return False
            
            data = response.json()
            
            if not data.get('success'):
                print(" FAILED! API returned success=false")
                return False
            
            # Process channels from channel_groups
            channel_groups = data.get('channel_groups', [])
            if channel_groups:
                for group in channel_groups:
                    for channel in group.get('channels', []):
                        cid = channel.get('cid')
                        title = channel.get('title', '')
                        
                        if cid and title and self._channel_is_selected(str(cid), title):
                            # Get logo URL
                            logo_url = ""
                            qualities = channel.get('qualities', [])
                            if qualities:
                                logo_url = qualities[0].get('logo_black_84', '').replace('84x48.png', '210x120.png')
                                # Convert relative URL to absolute URL
                                if logo_url.startswith('/'):
                                    logo_url = f"https://logos.zattic.com{logo_url}"
                            
                            self.channels[cid] = {
                                'title': title,
                                'logo': logo_url
                            }
            
            print(f" OK! ({len(self.channels)} channels)")
            if self.channel_filter:
                print(f"Channel filter active: {len(self.channel_filter)} requested entries")
            return len(self.channels) > 0
            
        except Exception as e:
            print(f" FAILED! Error: {e}")
            return False
    
    def download_epg_data(self, days: int = 7) -> bool:
        """Download EPG data for specified number of days.
        
        Args:
            days: Number of days to download (1-14)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            print(f"Downloading EPG data for {days} days...")
            
            # Calculate date range
            # Use the selected service timezone rather than the host/container
            # timezone. Docker commonly runs in UTC, while Zattoo's guide
            # windows need to start at 06:00 local time (including DST).
            base_date = datetime.now(self.timezone).replace(
                hour=6,
                minute=0,
                second=0,
                microsecond=0,
            )
            
            all_programs = []
            total_parts = days * 4
            current_part = 0
            
            for day in range(days):
                current_date = base_date + timedelta(days=day)
                
                # Split day into 4 parts (6-hour segments)
                for part in range(4):
                    current_part += 1
                    start_time = current_date + timedelta(hours=part * 6)
                    end_time = start_time + timedelta(hours=6)
                    
                    start_timestamp = int(start_time.timestamp())
                    end_timestamp = int(end_time.timestamp())
                    
                    # Show progress bar for EPG download
                    progress_bar = show_progress_bar(current_part - 1, total_parts, "Downloading EPG", 25)
                    print(f"\r{progress_bar} - Day {day + 1}, Part {part + 1}/4", end="", flush=True)
                
                    url = f"https://zattoo.com/zapi/v2/cached/program/power_guide/{self.power_guide_hash}"
                    params = {
                        'start': start_timestamp,
                        'end': end_timestamp
                    }
                    
                    response = self.session.get(url, params=params)
                    
                    if response.status_code != 200:
                        print(f"\r{progress_bar} - FAILED!")
                        continue
                    
                    data = response.json()
                    
                    if not data.get('success'):
                        print(f"\r{progress_bar} - FAILED!")
                        continue
                    
                    # Extract programs from channels array
                    channels_array = data.get('channels', [])
                    
                    # Process each channel's programs
                    for channel in channels_array:
                        cid = channel.get('cid') or channel.get('id')
                        channel_programs = channel.get('programs', [])
                        
                        if cid in self.channels and channel_programs:
                            for program in channel_programs:
                                # Add channel ID to program data
                                program['cid'] = cid
                                all_programs.append(program)
                    
                    # Update progress bar to show completion
                    progress_bar = show_progress_bar(current_part, total_parts, "Downloading EPG", 25)
                    print(f"\r{progress_bar} - Day {day + 1}, Part {part + 1}/4 OK", end="", flush=True)
                    time.sleep(0.1)  # Be nice to the API
            
        
            # Final progress bar update
            final_progress = show_progress_bar(total_parts, total_parts, "Downloading EPG", 25)
            print(f"\r{final_progress} - Complete!")
            print(f"Downloaded {len(all_programs)} programs total.")
            self.epg_data = all_programs
            return True
            
        except Exception as e:
            print(f"\nFAILED! Error: {e}")
            return False
    
    def get_program_details_batch(self, program_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Get detailed information for multiple programs in one request.
        
        Args:
            program_ids: List of Program IDs
            
        Returns:
            Dictionary mapping program_id to program details
        """
        try:
            url = f"https://zattoo.com/zapi/v2/cached/program/power_details/{self.power_guide_hash}"
            # Join multiple program IDs with comma
            params = {'program_ids': ','.join(program_ids)}
            
            request_start = time.time()
            response = self.session.get(url, params=params, timeout=30)
            request_time = time.time() - request_start
            
            if response.status_code != 200:
                print(f"\n  HTTP {response.status_code} for batch request (took {request_time:.2f}s)", end="", flush=True)
                return {}
            
            data = response.json()
            
            if not data.get('success'):
                print(f"\n  API returned success=false for batch request", end="", flush=True)
                return {}
            
            # Extract program details - programs are directly in the response
            programs = data.get('programs', {})
            
            return programs
            
        except requests.exceptions.Timeout:
            print(f"\n  Timeout for batch request", end="", flush=True)
            return {}
        except Exception as e:
            print(f"\n  Exception for batch request: {str(e)}", end="", flush=True)
            return {}

    def get_program_details(self, program_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed information for a specific program.
        
        Args:
            program_id: Program ID
            
        Returns:
            Program details or None if failed
        """
        batch_result = self.get_program_details_batch([program_id])
        return batch_result.get(program_id)
    
    def enhance_epg_data(self) -> None:
        """Enhance EPG data with detailed program information."""
        print("Enhancing EPG data with details...")
        start_time = time.time()

        programs_by_id: Dict[str, List[Dict[str, Any]]] = {}
        for program in self.epg_data:
            program_id = program.get('id')
            if program_id is not None:
                programs_by_id.setdefault(str(program_id), []).append(program)

        program_ids = list(programs_by_id)
        cached_details = self._load_cached_details(program_ids)
        missing_ids = [program_id for program_id in program_ids if program_id not in cached_details]
        print(
            f"Unique programs: {len(program_ids)}; cache hits: {len(cached_details)}; "
            f"API lookups: {len(missing_ids)}"
        )

        fetched_details: Dict[str, Dict[str, Any]] = {}
        batch_size = 20
        total_batches = (len(missing_ids) + batch_size - 1) // batch_size
        connection_errors = 0

        for offset in range(0, len(missing_ids), batch_size):
            batch = missing_ids[offset:offset + batch_size]
            current_batch = offset // batch_size + 1
            batch_details: Dict[str, Dict[str, Any]] = {}

            for retry_count in range(3):
                raw_details = self.get_program_details_batch(batch)
                batch_details = {
                    str(program_id): payload
                    for program_id, payload in raw_details.items()
                }
                if batch_details:
                    break
                if retry_count < 2:
                    time.sleep(2 ** (retry_count + 1))

            if not batch_details:
                connection_errors += 1
            else:
                fetched_details.update(batch_details)
                self._store_cached_details(batch_details)

            progress_bar = show_progress_bar(current_batch, total_batches, "Enhancing EPG", 30)
            print(
                f"\r{progress_bar} Batch {current_batch}: "
                f"{len(batch_details)}/{len(batch)} received",
                end="",
                flush=True,
            )
            time.sleep(0.5 if batch_details else 1.0)

            if connection_errors > 5:
                print("\nWARNING: Too many connection errors; stopping detail requests.")
                break

        if total_batches:
            print()

        all_details = {**cached_details, **fetched_details}
        enhanced_count = 0
        for program_id, details in all_details.items():
            for program in programs_by_id.get(program_id, []):
                program.update(details)
                enhanced_count += 1

        total_time = time.time() - start_time
        unique_success_rate = (len(all_details) / len(program_ids) * 100) if program_ids else 100
        print(f"Enhanced {enhanced_count} programme entries ({unique_success_rate:.1f}% unique coverage).")
        print(f"Connection errors: {connection_errors}")
        print(f"Total detail time: {total_time:.1f}s")
    
    def generate_xmltv(self, filename: Optional[str] = "zattoo_epg.xml", return_data: bool = False) -> Union[bool, str]:
        """Generate XMLTV file from collected EPG data.
        
        Args:
            filename: Output filename (None to skip file creation)
            return_data: If True, return XML data as string instead of boolean
            
        Returns:
            True/False if return_data is False, XML string if return_data is True
        """
        try:
            print(f"Generating XMLTV file: {filename}...")
            
            # Create root element
            tv_elem = ET.Element('tv')
            tv_elem.set('source-info-url', 'https://zattoo.com/')
            tv_elem.set('source-data-url', 'https://zattoo.com/')
            tv_elem.set('generator-info-name', f'Zattoo EPG Grabber Python')
            
            # Add comment
            comment_text = f' EPG XMLTV FILE CREATED BY ZATTOO EPG GRABBER - Generated on {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} '
            
            # Add channels
            print("Adding channels to XMLTV...")
            for cid, channel_info in self.channels.items():
                channel_elem = ET.SubElement(tv_elem, 'channel')
                channel_elem.set('id', cid)
                
                # Display name
                display_name = ET.SubElement(channel_elem, 'display-name')
                display_name.set('lang', self.language)
                display_name.text = channel_info['title']
                
                # Logo
                if channel_info.get('logo'):
                    icon_elem = ET.SubElement(channel_elem, 'icon')
                    icon_elem.set('src', channel_info['logo'])
            
            # Add programs
            print("Adding programs to XMLTV...")
            program_count = 0
            
            for program in self.epg_data:
                cid = program.get('cid')
                start_time = program.get('s')  # Start timestamp
                end_time = program.get('e')    # End timestamp
                title = program.get('t', '')   # Title
                
                if not all([cid, start_time, end_time, title]):
                    continue
                
                # Convert timestamps to XMLTV format
                start_dt = datetime.fromtimestamp(start_time, self.timezone)
                end_dt = datetime.fromtimestamp(end_time, self.timezone)
                
                start_str = start_dt.strftime('%Y%m%d%H%M%S %z')
                end_str = end_dt.strftime('%Y%m%d%H%M%S %z')
                
                # Create programme element
                programme_elem = ET.SubElement(tv_elem, 'programme')
                programme_elem.set('start', start_str)
                programme_elem.set('stop', end_str)
                programme_elem.set('channel', cid)
                
                # Title
                title_elem = ET.SubElement(programme_elem, 'title')
                title_elem.set('lang', self.language)
                title_elem.text = self._clean_text(title)
                
                # Subtitle
                subtitle = program.get('et')  # Episode title
                if subtitle:
                    subtitle_elem = ET.SubElement(programme_elem, 'sub-title')
                    subtitle_elem.set('lang', self.language)
                    subtitle_elem.text = self._clean_text(subtitle)
                
                # Description
                description = program.get('d')  # Description
                if description:
                    desc_elem = ET.SubElement(programme_elem, 'desc')
                    desc_elem.set('lang', self.language)
                    desc_elem.text = self._clean_text(description)
                
                # Image
                image = program.get('i_t')  # Image URL
                if image:
                    icon_elem = ET.SubElement(programme_elem, 'icon')
                    image_url = image if str(image).startswith(('http://', 'https://')) else (
                        f"https://images.zattic.com/cms/{image}/original.jpg"
                    )
                    icon_elem.set('src', image_url)
                
                # Year
                year = program.get('year')
                if year:
                    date_elem = ET.SubElement(programme_elem, 'date')
                    date_elem.text = str(year)
                
                # Country
                country = program.get('country')
                if country:
                    country_elem = ET.SubElement(programme_elem, 'country')
                    country_elem.text = country
                
                # Categories/Genres
                genres = program.get('g', [])  # Genre list
                for genre in genres:
                    if genre:
                        category_elem = ET.SubElement(programme_elem, 'category')
                        category_elem.set('lang', self.language)
                        category_elem.text = self._clean_text(genre)
                
                # Credits (cast and crew)
                credits = program.get('cr', {})
                if credits:
                    credits_elem = ET.SubElement(programme_elem, 'credits')
                    
                    # Directors
                    directors = credits.get('director', [])
                    for director in directors:
                        if director:
                            director_elem = ET.SubElement(credits_elem, 'director')
                            director_elem.text = self._clean_text(director)
                    
                    # Actors
                    actors = credits.get('actor', [])
                    for actor in actors:
                        if actor:
                            actor_elem = ET.SubElement(credits_elem, 'actor')
                            actor_elem.text = self._clean_text(actor)
                
                # Episode numbering
                series_num = program.get('s_no')
                episode_num = program.get('e_no')
                if series_num or episode_num:
                    episode_elem = ET.SubElement(programme_elem, 'episode-num')
                    episode_elem.set('system', 'xmltv_ns')
                    
                    series_str = str(int(series_num) - 1) if series_num else ''
                    episode_str = str(int(episode_num) - 1) if episode_num else ''
                    episode_elem.text = f"{series_str}.{episode_str}."
                
                # Rating
                rating = program.get('yp_r')
                if rating:
                    rating_elem = ET.SubElement(programme_elem, 'rating')
                    rating_elem.set('system', 'FSK')
                    value_elem = ET.SubElement(rating_elem, 'value')
                    value_elem.text = str(rating)
                
                program_count += 1
            
            print(f"Added {program_count} programs to XMLTV.")
            
            # Generate XML
            tree = ET.ElementTree(tv_elem)
            ET.indent(tree, space="  ", level=0)
            
            if return_data:
                # Return XML data as string
                import io
                xml_buffer = io.BytesIO()
                xml_buffer.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
                xml_buffer.write(b'<!DOCTYPE tv SYSTEM "xmltv.dtd">\n')
                tree.write(xml_buffer, encoding='utf-8')
                return xml_buffer.getvalue().decode('utf-8')
            
            elif filename:
                # Write atomically so TVHeadend never reads a partially written guide.
                output_path = Path(filename)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = output_path.with_name(f".{output_path.name}.tmp")
                with open(temporary_path, 'wb') as f:
                    f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
                    f.write(b'<!DOCTYPE tv SYSTEM "xmltv.dtd">\n')
                    tree.write(f, encoding='utf-8')
                os.replace(temporary_path, output_path)
                
                print(f"XMLTV file saved as: {filename}")
            
            return True
            
        except Exception as e:
            print(f"FAILED! Error: {e}")
            return False
    
    def _clean_text(self, text: str) -> str:
        """Clean text for XML output.
        
        Args:
            text: Input text
            
        Returns:
            Cleaned text
        """
        if not text:
            return ""
        
        # ElementTree performs XML escaping. Decode HTML entities and strip tags here.
        return html.unescape(re.sub(r'<[^>]*>', '', str(text))).strip()


def load_config(config_file: str = "config.json") -> Dict[str, str]:
    """Load configuration from JSON file.
    
    Args:
        config_file: Path to configuration file
        
    Returns:
        Dictionary containing configuration values
    """
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        return config
    except FileNotFoundError:
        print(f"Configuration file '{config_file}' not found!")
        print("Please create a config.json file with your email and password.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in configuration file: {e}")
        sys.exit(1)


def load_channel_filter(filter_file: Optional[str]) -> List[str]:
    """Load exact channel IDs or names, one per line."""
    if not filter_file:
        return []
    path = Path(filter_file)
    if not path.is_file():
        print(f"Channel filter file not found: {filter_file}")
        sys.exit(1)
    return [
        line.strip()
        for line in path.read_text(encoding='utf-8-sig').splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    ]


def send_xml_to_tvheadend(xml_data: str, socket_path: str = "/var/lib/tvheadend/epggrab/xmltv.sock") -> bool:
    """Send XMLTV data directly to TVHeadend via Unix socket.
    
    Args:
        xml_data: XML data as string
        socket_path: Path to TVHeadend's XMLTV socket
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Check if socket exists
        if not os.path.exists(socket_path):
            print(f"TVHeadend socket not found at {socket_path}")
            print("Make sure TVHeadend is running and XMLTV grabber is enabled.")
            return False
        
        print(f"Sending EPG data directly to TVHeadend via {socket_path}...")
        
        # Create Unix socket connection
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)
        
        # Send XML data
        sock.sendall(xml_data.encode('utf-8'))
        
        sock.close()
        print("Successfully sent EPG data to TVHeadend!")
        return True
        
    except socket.error as e:
        print(f"Socket error: {e}")
        print("Make sure TVHeadend is running and XMLTV grabber is configured.")
        return False
    except Exception as e:
        print(f"Failed to send to TVHeadend: {e}")
        return False


def send_to_tvheadend(xml_file: str, socket_path: str = "/var/lib/tvheadend/epggrab/xmltv.sock") -> bool:
    """Send XMLTV file to TVHeadend via Unix socket.
    
    Args:
        xml_file: Path to the XMLTV file
        socket_path: Path to TVHeadend's XMLTV socket
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Check if socket exists
        if not os.path.exists(socket_path):
            print(f"TVHeadend socket not found at {socket_path}")
            print("Make sure TVHeadend is running and XMLTV grabber is enabled.")
            return False
        
        # Check if XML file exists
        if not os.path.exists(xml_file):
            print(f"XML file not found: {xml_file}")
            return False
        
        print(f"Sending {xml_file} to TVHeadend via {socket_path}...")
        
        # Create Unix socket connection
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)
        
        # Read and send XML file
        with open(xml_file, 'rb') as f:
            data = f.read()
            sock.sendall(data)
        
        sock.close()
        print("Successfully sent EPG data to TVHeadend!")
        return True
        
    except socket.error as e:
        print(f"Socket error: {e}")
        print("Make sure TVHeadend is running and XMLTV grabber is configured.")
        return False
    except Exception as e:
        print(f"Failed to send to TVHeadend: {e}")
        return False


def show_progress_bar(current: int, total: int, prefix: str = "Progress", length: int = 40) -> str:
    """Generate a text-based progress bar.
    
    Args:
        current: Current progress value
        total: Total value
        prefix: Prefix text
        length: Length of the progress bar
        
    Returns:
        Progress bar string
    """
    if total == 0:
        percent = 100.0
    else:
        percent = (current / total) * 100
    
    filled = int(length * current // total) if total > 0 else 0
    bar = '█' * filled + '░' * (length - filled)
    
    return f"{prefix} |{bar}| {current}/{total} ({percent:.1f}%)"


def get_credentials_from_config(config_file: str = "config.json") -> tuple:
    """Get Zattoo login credentials from configuration file.
    
    Args:
        config_file: Path to configuration file
        
    Returns:
        Tuple of (username, password)
    """
    email = os.environ.get('ZATTOO_EMAIL', '').strip()
    password = os.environ.get('ZATTOO_PASSWORD', '')
    if email and password:
        return email, password

    config = load_config(config_file)
    email = config.get('email', '').strip()
    password = config.get('password', '')
    
    if not email:
        print("'email' not found or empty in configuration file!")
        sys.exit(1)
    
    if not password:
        print("'password' not found or empty in configuration file!")
        sys.exit(1)
    
    # Validate email format
    email_pattern = r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'
    if not re.match(email_pattern, email):
        print("Invalid email format in configuration file!")
        sys.exit(1)
    
    return email, password


def get_credentials() -> tuple:
    """Get Zattoo login credentials from user input.
    
    Returns:
        Tuple of (username, password)
    """
    print("=== Zattoo Login ===")
    
    username = input("Email address: ").strip()
    
    # Validate email format
    email_pattern = r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'
    if not re.match(email_pattern, username):
        print("Invalid email format!")
        sys.exit(1)
    
    password = getpass.getpass("Password: ")
    
    if not password:
        print("Password cannot be empty!")
        sys.exit(1)
    
    return username, password


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description='Zattoo EPG Grabber')
    parser.add_argument('--country', '-c', choices=['DE', 'CH'], default='DE',
                      help='Country code (DE or CH, default: DE)')
    parser.add_argument('--days', '-d', type=int, default=7, choices=range(1, 15),
                      help='Number of days to download (1-14, default: 7)')
    parser.add_argument('--output', '-o', default='zattoo_epg.xml',
                      help='Output filename (default: zattoo_epg.xml)')
    parser.add_argument('--no-details', action='store_true',
                      help='Skip downloading detailed program information (faster)')
    parser.add_argument('--config', default='config.json',
                      help='Configuration file; ZATTOO_EMAIL/ZATTOO_PASSWORD take precedence')
    parser.add_argument('--channel-filter',
                      help='Text file with exact channel IDs or names, one per line')
    parser.add_argument('--cache', default='cache/program-details.sqlite3',
                      help='SQLite detail cache (default: cache/program-details.sqlite3; empty disables)')
    parser.add_argument('--cache-ttl-days', type=int, default=30,
                      help='Detail cache lifetime in days (default: 30)')
    parser.add_argument('--interactive', action='store_true',
                      help='Use interactive login instead of config file')
    parser.add_argument('--debug', action='store_true',
                      help='Enable debug output for performance analysis')
    parser.add_argument('--tvheadend', action='store_true',
                      help='Send EPG data to TVHeadend after generation')
    parser.add_argument('--tvheadend-only', action='store_true',
                      help='Send EPG data directly to TVHeadend without saving to file')
    parser.add_argument('--tvheadend-socket', default='/var/lib/tvheadend/epggrab/xmltv.sock',
                      help='Path to TVHeadend XMLTV socket (default: /var/lib/tvheadend/epggrab/xmltv.sock)')
    
    args = parser.parse_args()
    
    print("=" * 50)
    print("ZATTOO EPG SIMPLE XMLTV GRABBER")
    print("Python Implementation")
    print("=" * 50)
    print()
    
    # Check if Zattoo service is available
    try:
        response = requests.get("https://zattoo.com/de", timeout=10)
        if response.status_code != 200:
            print("Zattoo service is not available!")
            sys.exit(1)
    except requests.RequestException:
        print("Cannot connect to Zattoo service!")
        sys.exit(1)
    
    # Get credentials
    if args.interactive:
        username, password = get_credentials()
    else:
        print(f"Loading credentials from: {args.config}")
        username, password = get_credentials_from_config(args.config)
        print(f"Loaded credentials for: {username}")
    print()
    
    # Initialize EPG grabber
    channel_filter = load_channel_filter(args.channel_filter)
    epg = ZattooEPG(
        country=args.country,
        channel_filter=channel_filter,
        cache_path=args.cache or None,
        cache_ttl_days=args.cache_ttl_days,
    )
    
    # Get session token
    if not epg.get_session_token():
        print("Failed to get session token!")
        sys.exit(1)
    
    # Login
    if not epg.login(username, password):
        print("Login failed! Please check your credentials.")
        sys.exit(1)
    
    print()
    
    # Get channels
    if not epg.get_channels():
        print("Failed to get channel list!")
        sys.exit(1)
    
    print()
    
    # Download EPG data
    download_start = time.time() if args.debug else 0
    if args.debug:
        print("Starting EPG data download...")
    
    if not epg.download_epg_data(days=args.days):
        print("Failed to download EPG data!")
        sys.exit(1)
    
    if args.debug:
        download_time = time.time() - download_start
        print(f"EPG download completed in {download_time:.1f}s")
    
    print()
    
    # Enhance with details (optional)
    if not args.no_details:
        enhance_start = time.time() if args.debug else 0
        if args.debug:
            print("Starting EPG enhancement...")
        
        epg.enhance_epg_data()
        
        if args.debug:
            enhance_time = time.time() - enhance_start
            print(f"EPG enhancement completed in {enhance_time:.1f}s")
        
        print()
    
    # Generate XMLTV or send directly to TVHeadend
    xmltv_start = time.time() if args.debug else 0
    if args.debug:
        print("Starting XMLTV generation...")
    
    if args.tvheadend_only:
        # Send directly to TVHeadend without saving to file
        xml_data = epg.generate_xmltv(filename=None, return_data=True)
        if not xml_data:
            print("Failed to generate XMLTV data!")
            sys.exit(1)
        
        if args.debug:
            xmltv_time = time.time() - xmltv_start
            print(f"XMLTV generation completed in {xmltv_time:.1f}s")
        
        print()
        if isinstance(xml_data, str) and send_xml_to_tvheadend(xml_data, args.tvheadend_socket):
            print("EPG data sent directly to TVHeadend.")
        else:
            print("Failed to send EPG data to TVHeadend.")
            sys.exit(1)
    else:
        # Generate file normally
        if not epg.generate_xmltv(filename=args.output):
            print("Failed to generate XMLTV file!")
            sys.exit(1)
        
        if args.debug:
            xmltv_time = time.time() - xmltv_start
            print(f"XMLTV generation completed in {xmltv_time:.1f}s")
        
        print()
        
        # Send to TVHeadend if requested
        if args.tvheadend:
            print()
            if send_to_tvheadend(args.output, args.tvheadend_socket):
                print("EPG data sent to TVHeadend successfully.")
            else:
                print("Failed to send EPG data to TVHeadend.")
    
    # Summary output
    if not args.tvheadend_only:
        print(f"Output file: {args.output}")
    print(f"Channels: {len(epg.channels)}")
    print(f"Programs: {len(epg.epg_data)}")
    
    if args.tvheadend or args.tvheadend_only:
        print("EPG data sent to TVHeadend.")


if __name__ == "__main__":
    main()
