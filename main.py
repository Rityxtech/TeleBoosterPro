import os
import sys
import csv
import time
import random
import asyncio
import datetime
from telethon.sync import TelegramClient
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.types import InputPeerEmpty, InputPeerChannel, InputPeerUser, UserStatusOnline, UserStatusRecently, UserStatusLastWeek, UserStatusOffline
from telethon.errors.rpcerrorlist import PeerFloodError, UserPrivacyRestrictedError, ChatWriteForbiddenError, FloodWaitError
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.contacts import AddContactRequest, GetContactsRequest, DeleteContactsRequest
from dotenv import load_dotenv

# --- Load Secure Config ---
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")

# --- Rate-Limiting Strategy ---
# Group adds   : burst of 5 per minute → 5-min rest → random re-entry within 1 min
# Contact adds : 50 contacts per burst, roughly 1-2s gap, 60s rest
GROUP_BURST_SIZE      = 5          # users per burst
GROUP_BURST_WINDOW    = 60         # seconds for one burst window
GROUP_REST_SECONDS    = 5 * 60     # mandatory rest after a burst (5 min)
GROUP_REENTRY_MAX     = 60         # random re-entry delay after rest (0-60 s)
CONTACT_BURST_SIZE    = 50         # contacts per burst
CONTACT_REST_SECONDS  = 60         # seconds of rest after burst

# Proxy IP Rotation Configuration (Useful with rotating residential proxies)
PROXY_TYPE = os.getenv("PROXY_TYPE", "").lower()
PROXY_ADDR = os.getenv("PROXY_ADDR", "")
PROXY_PORT = os.getenv("PROXY_PORT", "")
PROXY_USER = os.getenv("PROXY_USER", "")
PROXY_PASS = os.getenv("PROXY_PASS", "")

proxy_settings = None
if PROXY_TYPE and PROXY_ADDR and PROXY_PORT:
    try:
        proxy_settings = {
            'proxy_type': PROXY_TYPE, # 'socks5', 'socks4', or 'http'
            'addr': PROXY_ADDR,
            'port': int(PROXY_PORT),
            'rdns': True
        }
        if PROXY_USER and PROXY_PASS:
            proxy_settings['username'] = PROXY_USER
            proxy_settings['password'] = PROXY_PASS
        print(f"[*] Native Proxy Enabled: {PROXY_TYPE}://{PROXY_ADDR}:{PROXY_PORT}")
    except ValueError:
        print("[!] Warning: PROXY_PORT in .env is not a valid number. Running without proxy.")

if not API_ID or not API_HASH or not PHONE_NUMBER:
    print("[!] ERROR: Missing .env credentials.")
    sys.exit(1)

# Spoof Device Parameters to mimic an official iOS App
# This prevents Telegram from flagging default 'telethon' strings
client = TelegramClient(PHONE_NUMBER, int(API_ID), API_HASH,
                        proxy=proxy_settings,
                        device_model="iPhone 14 Pro",
                        system_version="iOS 16.5",
                        app_version="9.6.5",
                        lang_code="en",
                        system_lang_code="en-US")

async def countdown_timer(seconds, message="Waiting"):
    """Blocks and prints a live countdown in the terminal."""
    print() # New line
    for remaining in range(seconds, 0, -1):
        sys.stdout.write(f"\r => {message} {remaining}s remaining... ")
        sys.stdout.flush()
        await asyncio.sleep(1)
    sys.stdout.write("\r" + " " * 50 + "\r") # Clean up line
    sys.stdout.flush()

async def get_groups():
    """Retrieve all MegaGroups the user is part of."""
    chats = []
    last_date = None
    chunk_size = 200
    groups = []
    
    result = await client(GetDialogsRequest(
        offset_date=last_date,
        offset_id=0,
        offset_peer=InputPeerEmpty(),
        limit=chunk_size,
        hash=0
    ))
    chats.extend(result.chats)
    
    for chat in chats:
        try:
            if getattr(chat, 'megagroup', False) or getattr(chat, 'broadcast', False) == False:
                # We only want supergroups/megagroups
                if hasattr(chat, 'title'):
                    groups.append(chat)
        except:
            continue
    return groups

async def scrape_users():
    print("\n[+] Enter the source group link or username to SCRAPE users from.")
    print("Example: https://t.me/PublicGroup or @PublicGroup")
    
    target_group_url = input("\nEnter Group Link/Username: ").strip()
    if not target_group_url:
        print("Invalid input.")
        return

    try:
        print(f"\n[+] Fetching entity for {target_group_url}...")
        target_group = await client.get_entity(target_group_url)
    except Exception as e:
        print(f"[!] Could not find group. Error: {str(e)}")
        return

    print(f"\n[+] Fetching members of {getattr(target_group, 'title', target_group_url)}...")
    all_participants = []
    try:
        all_participants = await client.get_participants(target_group, limit=5000) # Prevents massive flood
    except Exception as e:
        print(f"[!] Could not fetch members. Are you admin or is the member list hidden? {str(e)}")
        return
    
    print(f"[+] Found {len(all_participants)} members. Saving to 'scraped_users.csv'...")
    with open("scraped_users.csv", "w", encoding='UTF-8', newline='') as f:
        writer = csv.writer(f, delimiter=",", lineterminator="\n")
        writer.writerow(['username', 'user_id', 'access_hash', 'name', 'group', 'group_id'])
        
        count = 0
        for user in all_participants:
            username = user.username if user.username else ""
            name = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
            
            # --- Active User Filter (Last 7 Days) ---
            is_active = False
            if hasattr(user, 'status'):
                if isinstance(user.status, (UserStatusOnline, UserStatusRecently, UserStatusLastWeek)):
                    is_active = True
                elif isinstance(user.status, UserStatusOffline) and hasattr(user.status, 'was_online'):
                    if user.status.was_online:
                        days_ago = (datetime.datetime.now(datetime.timezone.utc) - user.status.was_online).days
                        if days_ago <= 7:
                            is_active = True
            
            # Only save active users who have usernames
            if username and is_active:
                writer.writerow([username, user.id, user.access_hash, name, getattr(target_group, 'title', target_group_url), target_group.id])
                count += 1
                
    print(f"[+] Successfully saved {count} valid users to scraped_users.csv.")

async def add_users_to_group():
    users = []
    try:
        with open("scraped_users.csv", "r", encoding='UTF-8') as f:
            reader = csv.reader(f, delimiter=',')
            next(reader, None)  # Skip header
            for row in reader:
                if row:
                    users.append({'username': row[0], 'id': int(row[1]), 'access_hash': int(row[2])})
    except FileNotFoundError:
        print("[!] 'scraped_users.csv' not found. Please scrape users first!")
        return

    print("\n[+] Enter the target group link or username to ADD users to.")
    print("Example: https://t.me/MyTargetGroup or @MyTargetGroup")
    
    target_group_url = input("\nEnter Group Link/Username: ").strip()
    if not target_group_url:
        print("Invalid input.")
        return
    
    try:
        target_group = await client.get_entity(target_group_url)
    except Exception as e:
        print(f"[!] Could not find target group. Error: {str(e)}")
        return
    
    target_group_entity = InputPeerChannel(target_group.id, target_group.access_hash)

    # --- State Management: Global Tracking ---
    processed_file = f"processed_{target_group.id}.txt"
    processed_users = set()
    # Read ALL history to avoid retrying users added to contacts or other groups
    for file in os.listdir("."):
        if file.startswith("processed_") and file.endswith(".txt"):
            with open(file, "r", encoding="utf-8") as pf:
                for line in pf:
                    line_id = line.strip()
                    if line_id:
                        processed_users.add(line_id)
            
    print("\n[+] Bypassing pre-fetch of current members to reduce API stress. Users already in the group will be skipped dynamically.")
    print(f"\n[+] Total skipped (Already members + previously processed): {len(processed_users)}")
    print(f"\n[+] Rate strategy: {GROUP_BURST_SIZE} users/min → {GROUP_REST_SECONDS // 60}-min rest → random re-entry within {GROUP_REENTRY_MAX}s")
    print("[+] Press Ctrl+C to stop at any time.")
    
    total_added  = 0          # overall session counter
    burst_count  = 0          # adds in the current 60-s burst
    burst_start  = time.time() # when the current burst began
    peer_flood_counter = 0

    # Daily hard cap - Telegram typically restricts heavily above ~30 adds/day per account
    DAILY_CAP = 30
    
    for user in users:
        user_id_str = str(user['id'])
        if user_id_str in processed_users:
            continue

        if total_added >= DAILY_CAP:
            print(f"\n[+] Daily cap of {DAILY_CAP} reached. Stopping to protect the account.")
            break
            
        try:
            print(f"Adding {user['username']}...")
            user_to_add = await client.get_input_entity(user['username'])
            await client(InviteToChannelRequest(target_group_entity, [user_to_add]))
            
            # Record success
            with open(processed_file, "a", encoding="utf-8") as pf:
                pf.write(user_id_str + "\n")
            processed_users.add(user_id_str)
            peer_flood_counter = 0

            total_added += 1
            burst_count += 1
            print(f" => Success! [{burst_count}/{GROUP_BURST_SIZE} in burst | {total_added} total]")

            # --- BURST RATE LIMITER ---
            if burst_count >= GROUP_BURST_SIZE:
                # We just completed a full burst; enforce 5-min rest
                print(f"\n => [BURST COMPLETE] {GROUP_BURST_SIZE} users added this minute.")
                print(f" => Resting for {GROUP_REST_SECONDS // 60} minutes to stay under Telegram's radar...")
                await countdown_timer(GROUP_REST_SECONDS, "Rest Period")

                # Random re-entry delay (0 – 60 s) to simulate human unpredictability
                reentry_delay = random.randint(0, GROUP_REENTRY_MAX)
                if reentry_delay > 0:
                    print(f" => [RE-ENTRY] Waiting an extra {reentry_delay}s before starting next burst...")
                    await countdown_timer(reentry_delay, "Re-entry delay")

                # Reset burst window
                burst_count = 0
                burst_start = time.time()
            else:
                # Within the burst: spread remaining slots evenly across the 60-s window
                # Time already consumed in this burst
                elapsed = time.time() - burst_start
                # How many adds remain in this burst (including the one just done)
                remaining_slots = GROUP_BURST_SIZE - burst_count
                # Remaining time in the 60-s window
                remaining_window = max(GROUP_BURST_WINDOW - elapsed, 0)
                if remaining_slots > 0 and remaining_window > 0:
                    # Evenly spread + small jitter so we don't hammer at exact intervals
                    base_gap = remaining_window / remaining_slots
                    jitter    = random.uniform(-2, 2)
                    gap       = max(3, base_gap + jitter)  # never less than 3 s
                else:
                    gap = random.uniform(3, 8)
                print(f" => Waiting {gap:.1f}s before next add (burst pacing)...")
                await countdown_timer(int(gap), "Burst gap")

        except FloodWaitError as e:
            print(f"\n[!] Explicit Rate Limit: Telegram says wait {e.seconds}s.")
            await countdown_timer(e.seconds, "Flood Wait")
            continue
        except PeerFloodError:
            peer_flood_counter += 1
            print(f"\n[!] PeerFloodError on {user['username']} (Strike {peer_flood_counter}/3)")
            with open(processed_file, "a", encoding="utf-8") as pf:
                pf.write(user_id_str + "\n")
            processed_users.add(user_id_str)
            if peer_flood_counter >= 3:
                print("\n[!] CRITICAL: 3 consecutive Flood errors. Wait 24 hours before reusing this account.")
                break
            else:
                print(" => Cooling down 2 minutes before next attempt...")
                await countdown_timer(120, "Cooling down")
                continue
        except UserPrivacyRestrictedError:
            print(f" => {user['username']} has strict privacy settings. Skipping permanently.")
            with open(processed_file, "a", encoding="utf-8") as pf:
                pf.write(user_id_str + "\n")
            processed_users.add(user_id_str)
            continue
        except ChatWriteForbiddenError:
            print("\n[!] No permission to add users to this group.")
            break
        except Exception as e:
            if 'privacy' in str(e).lower() or 'already' in str(e).lower() or 'participant' in str(e).lower():
                with open(processed_file, "a", encoding="utf-8") as pf:
                    pf.write(user_id_str + "\n")
                processed_users.add(user_id_str)
                if 'privacy' in str(e).lower():
                    print(f" => {user['username']} privacy settings reject additions.")
                else:
                    print(f" => Already in the group. Skipping.")
            else:
                print(f" => Unexpected error: {str(e)}")
            continue

async def add_users_to_contacts():
    users = []
    try:
        with open("scraped_users.csv", "r", encoding='UTF-8') as f:
            reader = csv.reader(f, delimiter=',')
            next(reader, None)  # Skip header
            for row in reader:
                if row:
                    users.append({'username': row[0], 'id': int(row[1]), 'access_hash': int(row[2]), 'name': row[3]})
    except FileNotFoundError:
        print("[!] 'scraped_users.csv' not found. Please scrape users first!")
        return

    print(f"\n[+] Processing {len(users)} users to be added to your Telegram Contacts...")
    print(f"[+] Rate strategy: add {CONTACT_BURST_SIZE} contacts (~1-2s gap each) → {CONTACT_REST_SECONDS}s rest")
    print("[+] Press Ctrl+C to stop at any time.")
    
    processed_file = "processed_contacts.txt"
    processed_contacts = set()
    # Read ALL history to avoid retrying those already added to a group or skipped
    for file in os.listdir("."):
        if file.startswith("processed_") and file.endswith(".txt"):
            with open(file, "r", encoding="utf-8") as pf:
                for line in pf:
                    line_id = line.strip()
                    if line_id:
                        processed_contacts.add(line_id)
            
    print(f"[+] Skipping {len(processed_contacts)} already attempted contacts.")
    
    count            = 0
    burst_count      = 0
    burst_start      = time.time()
    peer_flood_counter = 0
    
    for user in users:
        user_id_str = str(user['id'])
        if user_id_str in processed_contacts:
            continue
            
        try:
            print(f"Adding {user['username']} to Contacts...")
            user_to_add = await client.get_input_entity(user['username'])
            
            name_parts = user['name'].split(" ", 1)
            first_name = name_parts[0] if name_parts else "Contact"
            last_name  = name_parts[1] if len(name_parts) > 1 else ""
            
            await client(AddContactRequest(
                id=user_to_add,
                first_name=first_name,
                last_name=last_name,
                phone='',
                add_phone_privacy_exception=False
            ))
            
            with open(processed_file, "a", encoding="utf-8") as pf:
                pf.write(user_id_str + "\n")
            processed_contacts.add(user_id_str)
            peer_flood_counter = 0

            count       += 1
            burst_count += 1
            print(f" => Success! [{burst_count}/{CONTACT_BURST_SIZE} this minute | {count} total]")

            # --- CONTACT RATE LIMITER (50 per burst, 1s gap) ---
            if burst_count >= CONTACT_BURST_SIZE:
                print(f"\n => [BURST COMPLETE] Added {CONTACT_BURST_SIZE} contacts.")
                print(f" => Resting for {CONTACT_REST_SECONDS} seconds for safety...")
                await countdown_timer(CONTACT_REST_SECONDS, "Burst rest")
                burst_count = 0
            else:
                gap = random.uniform(1.0, 2.0)
                print(f" => Waiting {gap:.1f}s before next contact...")
                await countdown_timer(int(gap) if int(gap) > 0 else 1, "Pacing")

        except FloodWaitError as e:
            print(f"\n[!] Rate Limit: Telegram says wait {e.seconds}s.")
            await countdown_timer(e.seconds, "Flood Wait")
            continue
        except PeerFloodError:
            peer_flood_counter += 1
            print(f"\n[!] PeerFloodError - Contact adding limited. (Strike {peer_flood_counter}/3)")
            with open(processed_file, "a", encoding="utf-8") as pf:
                pf.write(user_id_str + "\n")
            processed_contacts.add(user_id_str)
            if peer_flood_counter >= 3:
                print("\n[!] CRITICAL: 3 consecutive errors. Breaking.")
                break
            else:
                await countdown_timer(120, "Cooling down")
                continue
        except Exception as e:
            print(f" => Unexpected error or Privacy blockage: {str(e)}")
            with open(processed_file, "a", encoding="utf-8") as pf:
                pf.write(user_id_str + "\n")
            processed_contacts.add(user_id_str)
            continue

async def clear_saved_data():
    print("\n--- DATA CLEARANCE MENU ---")
    print("1. Wipe ALL Data (Deletes scraped_users.csv AND all processing history)")
    print("2. Delete ONLY scraped_users.csv (Keeps history to prevent future retries)")
    print("3. Clean scraped_users.csv (Removes ONLY processed/skipped users from the file)")
    print("4. Cancel")
    
    choice = input("\nEnter Choice (1, 2, 3, or 4): ").strip()
    
    if choice == '1':
        print("\n[+] Wiping all saved users and history...")
        count = 0
        if os.path.exists("scraped_users.csv"):
            os.remove("scraped_users.csv")
            count += 1
            print(" => Deleted scraped_users.csv")
            
        for file in os.listdir("."):
            if file.startswith("processed_") and file.endswith(".txt"):
                os.remove(file)
                count += 1
                print(f" => Deleted history: {file}")
                
        if count > 0:
            print(f"\n[+] Successfully wiped {count} file(s). Complete fresh start ready!")
        else:
            print("\n[-] No data files found. Already starting fresh.")

    elif choice == '2':
        if os.path.exists("scraped_users.csv"):
            os.remove("scraped_users.csv")
            print("\n[+] Successfully deleted scraped_users.csv")
            print("[+] Processing history was KEPT safely. You won't retry previously processed users.")
        else:
            print("\n[-] 'scraped_users.csv' not found. Nothing to delete.")

    elif choice == '3':
        if not os.path.exists("scraped_users.csv"):
            print("\n[-] 'scraped_users.csv' does not exist. Nothing to clean.")
            return
            
        # Gather all processed IDs
        processed_ids = set()
        for file in os.listdir("."):
            if file.startswith("processed_") and file.endswith(".txt"):
                with open(file, "r", encoding="utf-8") as pf:
                    for line in pf:
                        line_id = line.strip()
                        if line_id:
                            processed_ids.add(line_id)
                            
        if not processed_ids:
            print("\n[-] No processing history found. No users were removed from the list.")
            return
            
        # Read the CSV and filter out processed users
        kept_rows = []
        removed_count = 0
        total_rows = 0
        header = []
        
        with open("scraped_users.csv", "r", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=",")
            try:
                header = next(reader)
            except StopIteration:
                pass
            
            for row in reader:
                if not row: continue
                total_rows += 1
                # Ensure row has enough columns to represent user_id at index 1
                if len(row) > 1:
                    user_id = str(row[1]).strip()
                    if user_id in processed_ids:
                        removed_count += 1
                    else:
                        kept_rows.append(row)
                else:
                    kept_rows.append(row)
                    
        # Write back to CSV
        with open("scraped_users.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, delimiter=",", lineterminator="\n")
            if header:
                writer.writerow(header)
            writer.writerows(kept_rows)
            
        print(f"\n[+] Successfully cleaned 'scraped_users.csv':")
        print(f" => Original list : {total_rows} users")
        print(f" => Removed       : {removed_count} already processed/skipped users")
        print(f" => Remaining     : {len(kept_rows)} untouched users")

    elif choice == '4':
        print("\n[-] Operation cancelled.")
    else:
        print("\n[!] Invalid choice.")

async def clear_telegram_contacts():
    print("\n[!] WARNING: This will delete ALL contacts from your Telegram account.")
    confirm = input("Are you absolutely sure you want to clear your contact list to zero? (y/n): ").strip().lower()
    
    if confirm != 'y':
        print("[-] Operation cancelled.")
        return
        
    print("\n[+] Fetching your Telegram contacts...")
    try:
        # hash=0 fetches all contacts
        result = await client(GetContactsRequest(hash=0))
        contacts = result.users
        
        if not contacts:
            print("[-] Your contact list is already empty.")
            return
            
        print(f"[+] Found {len(contacts)} contacts. Deleting them now...")
        
        count = 0
        chunk_size = 50
        # Delete in chunks to avoid overwhelming the API
        for i in range(0, len(contacts), chunk_size):
            chunk = contacts[i:i + chunk_size]
            await client(DeleteContactsRequest(id=chunk))
            count += len(chunk)
            print(f" => Deleted {count}/{len(contacts)} contacts...")
            await asyncio.sleep(1)
            
        print("\n[+] Successfully cleared all contacts to zero!")
        
    except FloodWaitError as e:
        print(f"\n[!] Rate Limit: Telegram says wait {e.seconds}s before deleting more.")
    except Exception as e:
        print(f"\n[!] Error clearing contacts: {str(e)}")


async def main():
    print("\n" + "="*50)
    print(" 🚀 SECURE TELEGRAM ADDER (Production Build)")
    print("="*50)
    
    while True:
        print("\n--- MENU ---")
        print("1. Scrape Users from a Source Group")
        print("2. Add Scraped Users to my Target Group")
        print("3. Add Scraped Users to Contacts")
        print("4. Clear Saved Users & History (Fresh Start)")
        print("5. Clear Telegram Contacts (Wipe contacts to zero)")
        print("6. Logout Current Session (Switch Account)")
        print("7. Exit")
        
        choice = input("\nEnter Choice (1-7): ").strip()
        
        if choice == '1':
            await scrape_users()
        elif choice == '2':
            await add_users_to_group()
        elif choice == '3':
            await add_users_to_contacts()
        elif choice == '4':
            await clear_saved_data()
        elif choice == '5':
            await clear_telegram_contacts()
        elif choice == '6':
            confirm = input("\n[?] Are you sure you want to log out? This deletes the active session from your device! (y/n): ").strip().lower()
            if confirm == 'y':
                print("\n[+] Logging out in progress...")
                await client.log_out()
                print("[+] Successfully logged out. The session file has been deleted.")
                print("[+] To use a new account, update your .env with a new PHONE_NUMBER and run the bot again.")
                break
            else:
                print("[-] Logout cancelled.")
        elif choice == '7':
            print("Exiting securely...")
            break
        else:
            print("Invalid choice, please select 1-7.")

if __name__ == '__main__':
    with client:
        client.loop.run_until_complete(main())
