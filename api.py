import os
import sys
import csv
import time
import random
import asyncio
import datetime
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

from telethon.sync import TelegramClient
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.types import InputPeerEmpty, InputPeerChannel, UserStatusOnline, UserStatusRecently, UserStatusLastWeek, UserStatusOffline
from telethon.errors.rpcerrorlist import PeerFloodError, UserPrivacyRestrictedError, ChatWriteForbiddenError, FloodWaitError
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.contacts import AddContactRequest, GetContactsRequest, DeleteContactsRequest
from dotenv import load_dotenv

load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

GROUP_BURST_SIZE      = 5
GROUP_BURST_WINDOW    = 60
GROUP_REST_SECONDS    = 5 * 60
GROUP_REENTRY_MAX     = 60
CONTACT_BURST_SIZE    = 50
CONTACT_REST_SECONDS  = 60

PROXY_TYPE = os.getenv("PROXY_TYPE", "").lower()
PROXY_ADDR = os.getenv("PROXY_ADDR", "")
PROXY_PORT = os.getenv("PROXY_PORT", "")
PROXY_USER = os.getenv("PROXY_USER", "")
PROXY_PASS = os.getenv("PROXY_PASS", "")

proxy_settings = None
if PROXY_TYPE and PROXY_ADDR and PROXY_PORT:
    try:
        proxy_settings = {
            'proxy_type': PROXY_TYPE,
            'addr': PROXY_ADDR,
            'port': int(PROXY_PORT),
            'rdns': True
        }
        if PROXY_USER and PROXY_PASS:
            proxy_settings['username'] = PROXY_USER
            proxy_settings['password'] = PROXY_PASS
    except ValueError:
        pass

if not API_ID or not API_HASH:
    print("[!] ERROR: Missing .env credentials (API_ID, API_HASH).")
    sys.exit(1)

# Dynamically find the active phone number
active_phone = os.getenv("PHONE_NUMBER", "")
if not active_phone:
    # Try to find an existing session
    for file in os.listdir("."):
        if file.endswith(".session"):
            active_phone = file.replace(".session", "")
            break

client: Optional[TelegramClient] = None

def get_base_client(phone: str) -> TelegramClient:
    return TelegramClient(phone, int(API_ID), API_HASH,
                          proxy=proxy_settings,
                          device_model="iPhone 14 Pro",
                          system_version="iOS 16.5",
                          app_version="9.6.5",
                          lang_code="en",
                          system_lang_code="en-US")

if active_phone:
    client = get_base_client(active_phone)
else:
    print("[!] No PHONE_NUMBER provided and no sessions found. Application needs an account assigned.")

# Background job state
active_task = None
current_running_task: Optional[asyncio.Task] = None
connected_websockets: List[WebSocket] = []
logs_history: List[str] = []
temp_auth_clients = {} # phone -> dict("client", "hash")

def load_restricted():
    if not os.path.exists("restricted.json"): return {}
    try:
        with open("restricted.json", "r") as f:
            return json.load(f)
    except: return {}

def save_restricted(data):
    with open("restricted.json", "w") as f:
        json.dump(data, f)

def mark_restricted(phone):
    r = load_restricted()
    r[phone] = time.time()
    save_restricted(r)

def unmark_restricted(phone):
    r = load_restricted()
    if phone in r:
        del r[phone]
        save_restricted(r)

async def rotate_account() -> str:
    global client, active_phone
    restricted = load_restricted()
    sessions = sorted([f.replace(".session", "") for f in os.listdir(".") if f.endswith(".session")])
    if not sessions: return ""
    
    idx = sessions.index(active_phone) if active_phone in sessions else -1
    for i in range(1, len(sessions) + 1):
        cand = sessions[(idx + i) % len(sessions)]
        if cand not in restricted:
            if client and client.is_connected():
                await client.disconnect()
            active_phone = cand
            client = get_base_client(cand)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                if os.path.exists(f"{cand}.session"): os.remove(f"{cand}.session")
                if os.path.exists(f"{cand}.session-journal"): os.remove(f"{cand}.session-journal")
                continue
            await emit_log(f"[*] Native Rotation: Switched active session to {cand}")
            return cand
    return ""

async def emit_log(msg: str):
    msg_str = str(msg)
    print(msg_str)
    logs_history.append(msg_str)
    if len(logs_history) > 1000:
        logs_history.pop(0)
    
    dead_ws = []
    for ws in connected_websockets:
        try:
            await ws.send_text(msg_str)
        except Exception:
            dead_ws.append(ws)
    
    for d in dead_ws:
        if d in connected_websockets:
            connected_websockets.remove(d)

async def countdown_timer(seconds, message="Waiting"):
    await emit_log(f"\n=> {message} {seconds}s begun...")
    for _ in range(int(seconds)):
        await asyncio.sleep(1)
    await emit_log(f"=> {message} completed.")

async def do_scrape_users(target_group_url: str):
    global active_task, current_running_task
    try:
        await emit_log(f"[+] Fetching entity for {target_group_url}...")
        try:
            target_group = await client.get_entity(target_group_url)
        except Exception as e:
            await emit_log(f"[!] Could not find group. Error: {str(e)}")
            return

        title = getattr(target_group, 'title', target_group_url)
        await emit_log(f"[+] Fetching members of {title}...")
        all_participants = []
        try:
            all_participants = await client.get_participants(target_group, limit=5000)
        except Exception as e:
            await emit_log(f"[!] Could not fetch members. Error: {str(e)}")
            return
        
        await emit_log(f"[+] Found {len(all_participants)} members. Saving...")
        with open("scraped_users.csv", "w", encoding='UTF-8', newline='') as f:
            writer = csv.writer(f, delimiter=",", lineterminator="\n")
            writer.writerow(['username', 'user_id', 'access_hash', 'name', 'group', 'group_id'])
            
            count = 0
            for user in all_participants:
                username = user.username if user.username else ""
                name = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
                
                is_active = False
                if hasattr(user, 'status') and user.status:
                    status_name = type(user.status).__name__
                    # Accept users active within ~30 days
                    if status_name in ('UserStatusOnline', 'UserStatusRecently', 'UserStatusLastWeek', 'UserStatusLastMonth'):
                        is_active = True
                    elif status_name == 'UserStatusOffline' and hasattr(user.status, 'was_online'):
                        if user.status.was_online:
                            days_ago = (datetime.datetime.now(datetime.timezone.utc) - user.status.was_online).days
                            if days_ago <= 30:
                                is_active = True
                
                if username and is_active:
                    writer.writerow([username, user.id, user.access_hash, name, title, target_group.id])
                    count += 1
                    
        await emit_log(f"[+] Successfully saved {count} valid users to scraped_users.csv.")
    except asyncio.CancelledError:
        await emit_log("[!] Task was CANCELLED by the user.")
        raise
    except Exception as e:
        await emit_log(f"[!] Critical Error: {str(e)}")
    finally:
        active_task = None
        current_running_task = None
        await emit_log("[+] Scrape Task Finished.")

async def do_add_group(target_group_url: str):
    global active_task, current_running_task
    try:
        users = []
        try:
            with open("scraped_users.csv", "r", encoding='UTF-8') as f:
                reader = csv.reader(f, delimiter=',')
                next(reader, None)
                for row in reader:
                    if row:
                        users.append({'username': row[0], 'id': int(row[1]), 'access_hash': int(row[2])})
        except FileNotFoundError:
            await emit_log("[!] 'scraped_users.csv' not found. Please scrape users first!")
            return

        try:
            target_group = await client.get_entity(target_group_url)
        except Exception as e:
            await emit_log(f"[!] Could not find target group. Error: {str(e)}")
            return
        
        target_group_entity = InputPeerChannel(target_group.id, target_group.access_hash)

        processed_file = f"processed_{target_group.id}.txt"
        processed_users = set()
        for file in os.listdir("."):
            if file.startswith("processed_") and file.endswith(".txt"):
                with open(file, "r", encoding="utf-8") as pf:
                    for line in pf:
                        line_id = line.strip()
                        if line_id:
                            processed_users.add(line_id)
                
        await emit_log(f"[+] Total skipped (Already members + previously processed): {len(processed_users)}")
        
        total_added  = 0
        burst_count  = 0
        
        DAILY_CAP = 200 # Bumped up since we do multi-account
        
        user_idx = 0
        while user_idx < len(users):
            user = users[user_idx]
            user_id_str = str(user['id'])
            
            if user_id_str in processed_users:
                user_idx += 1
                continue

            if total_added >= DAILY_CAP:
                await emit_log(f"[+] Daily cap of {DAILY_CAP} reached. Stopping.")
                break
                
            if burst_count >= 10:
                await emit_log(f"=> [REST] 10 additions complete. Resting for 5 minutes.")
                await countdown_timer(300, "5-Minute Burst Rest")
                
                await emit_log(f"=> [ROTATE] Attempting to rotate session...")
                next_p = await rotate_account()
                if not next_p:
                    await emit_log("[!] No other unrestricted accounts available. Continuing on same account...")
                else:
                    await emit_log(f"=> [ROTATE] Successfully rotated to {active_phone}. Resuming execution...")
                burst_count = 0

            try:
                await emit_log(f"Adding {user['username']} via {active_phone}...")
                user_to_add = await client.get_input_entity(user['username'])
                await client(InviteToChannelRequest(target_group_entity, [user_to_add]))
                
                with open(processed_file, "a", encoding="utf-8") as pf:
                    pf.write(user_id_str + "\n")
                processed_users.add(user_id_str)
                
                total_added += 1
                burst_count += 1
                user_idx += 1
                
                gap = random.uniform(20.0, 40.0)
                await emit_log(f" => Success! [{total_added} cumulative]. Waiting {gap:.1f}s...")
                await countdown_timer(int(gap), "Inter-add pacing")
                
            except FloodWaitError as e:
                await emit_log(f"[!] Explicit Rate Limit on {active_phone}: Telegram says wait {e.seconds}s.")
                await countdown_timer(e.seconds, "Flood Wait")
                continue
            except PeerFloodError:
                await emit_log(f"[!] PEER FLOOD LIMIT reached on {active_phone}. Marking restricted!")
                mark_restricted(active_phone)
                next_p = await rotate_account()
                if not next_p:
                    await emit_log("[!] CRITICAL: All available accounts are PeerFlooded. Halting task.")
                    break
                burst_count = 0
                continue
            except UserPrivacyRestrictedError:
                await emit_log(f" => {user['username']} has strict privacy. Skipping.")
                with open(processed_file, "a", encoding="utf-8") as pf:
                    pf.write(user_id_str + "\n")
                processed_users.add(user_id_str)
                user_idx += 1
                continue
            except ChatWriteForbiddenError:
                await emit_log("[!] No permission to add users to this group.")
                break
            except Exception as e:
                err_str = str(e).lower()
                if 'privacy' in err_str or 'already' in err_str or 'participant' in err_str:
                    with open(processed_file, "a", encoding="utf-8") as pf:
                        pf.write(user_id_str + "\n")
                    processed_users.add(user_id_str)
                    if 'privacy' in err_str:
                        await emit_log(f" => {user['username']} privacy settings reject.")
                    else:
                        await emit_log(f" => Already in the group. Skipping.")
                elif 'too many requests' in err_str or 'wait' in err_str:
                     await emit_log(f"[!] Rate limit hit on {active_phone}. Marking restricted.")
                     mark_restricted(active_phone)
                     next_p = await rotate_account()
                     if not next_p: break
                     burst_count = 0
                     continue
                else:
                    await emit_log(f" => Unexpected error: {str(e)}")
                user_idx += 1
                continue
    except asyncio.CancelledError:
        await emit_log("[!] Task was CANCELLED by the user.")
        raise
    except Exception as e:
        await emit_log(f"[!] CRITICAL: {str(e)}")
    finally:
        active_task = None
        current_running_task = None
        await emit_log("[+] Add Users Task Finished.")

async def do_add_contacts():
    global active_task, current_running_task
    try:
        users = []
        try:
            with open("scraped_users.csv", "r", encoding='UTF-8') as f:
                reader = csv.reader(f, delimiter=',')
                next(reader, None)
                for row in reader:
                    if row:
                        users.append({'username': row[0], 'id': int(row[1]), 'access_hash': int(row[2]), 'name': row[3]})
        except FileNotFoundError:
            await emit_log("[!] 'scraped_users.csv' not found. Please scrape users first!")
            return

        processed_file = "processed_contacts.txt"
        processed_contacts = set()
        for file in os.listdir("."):
            if file.startswith("processed_") and file.endswith(".txt"):
                with open(file, "r", encoding="utf-8") as pf:
                    for line in pf:
                        line_id = line.strip()
                        if line_id:
                            processed_contacts.add(line_id)
                
        await emit_log(f"[+] Skipping {len(processed_contacts)} already attempted contacts.")
        
        count            = 0
        burst_count      = 0
        peer_flood_counter = 0
        
        for user in users:
            user_id_str = str(user['id'])
            if user_id_str in processed_contacts:
                continue
                
            try:
                await emit_log(f"Adding {user['username']} to Contacts...")
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
                await emit_log(f" => Success! [{burst_count}/{CONTACT_BURST_SIZE} | {count} total]")

                if burst_count >= CONTACT_BURST_SIZE:
                    await emit_log(f"=> [BURST COMPLETE] Resting {CONTACT_REST_SECONDS}s...")
                    await countdown_timer(CONTACT_REST_SECONDS, "Burst rest")
                    burst_count = 0
                else:
                    gap = random.uniform(1.0, 2.0)
                    await countdown_timer(int(gap) if int(gap) > 0 else 1, "Pacing")

            except FloodWaitError as e:
                await emit_log(f"[!] Rate Limit: Telegram says wait {e.seconds}s.")
                await countdown_timer(e.seconds, "Flood Wait")
                continue
            except PeerFloodError:
                peer_flood_counter += 1
                await emit_log(f"[!] PeerFloodError. (Strike {peer_flood_counter}/3)")
                with open(processed_file, "a", encoding="utf-8") as pf:
                    pf.write(user_id_str + "\n")
                processed_contacts.add(user_id_str)
                if peer_flood_counter >= 3:
                    await emit_log("[!] CRITICAL: 3 consecutive errors. Breaking.")
                    break
                else:
                    await countdown_timer(120, "Cooling down")
                    continue
            except Exception as e:
                await emit_log(f" => Expected error/Privacy: {str(e)}")
                with open(processed_file, "a", encoding="utf-8") as pf:
                    pf.write(user_id_str + "\n")
                processed_contacts.add(user_id_str)
                continue
    except asyncio.CancelledError:
        await emit_log("[!] Task was CANCELLED by the user.")
        raise
    except Exception as e:
        await emit_log(f"[!] CRITICAL: {str(e)}")
    finally:
        active_task = None
        current_running_task = None
        await emit_log("[+] Add Contacts Task Finished.")

async def do_clear_contacts():
    global active_task, current_running_task
    await emit_log("[+] Fetching contacts to delete...")
    try:
        result = await client(GetContactsRequest(hash=0))
        contacts = result.users
        if not contacts:
            await emit_log("[-] Contact list is already empty.")
            return
        
        await emit_log(f"[+] Found {len(contacts)} contacts. Deleting...")
        count = 0
        chunk_size = 50
        for i in range(0, len(contacts), chunk_size):
            chunk = contacts[i:i + chunk_size]
            await client(DeleteContactsRequest(id=chunk))
            count += len(chunk)
            await emit_log(f" => Deleted {count}/{len(contacts)}...")
            await asyncio.sleep(1)
        await emit_log("[+] All contacts cleared!")
    except asyncio.CancelledError:
        await emit_log("[!] Task was CANCELLED by the user.")
        raise
    except Exception as e:
        await emit_log(f"[!] Error: {str(e)}")
    finally:
        active_task = None
        current_running_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    if client:
        await client.connect()
        if not await client.is_user_authorized():
            print("[!] ERROR: Client not authorized. Please authenticate using the UI.")
    yield
    if client:
        await client.disconnect()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ScrapeReq(BaseModel):
    url: str

class TargetReq(BaseModel):
    url: str

class ClearReq(BaseModel):
    choice: str

class AuthPhoneReq(BaseModel):
    phone: str

class AuthCodeReq(BaseModel):
    phone: str
    code: str

@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    connected_websockets.append(websocket)
    for log in logs_history[-50:]:
        await websocket.send_text(log)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)

@app.get("/api/status")
async def get_status():
    return {"active_task": active_task}

@app.post("/api/stop")
async def api_stop():
    global active_task, current_running_task
    if current_running_task and not current_running_task.done():
        current_running_task.cancel()
        current_running_task = None
        active_task = None
        await emit_log("[+] Stop command executed immediately.")
        return {"status": "stopped"}
    return {"status": "none"}

@app.get("/api/accounts")
async def api_get_accounts():
    accounts = []
    restricted = load_restricted()
    for f in os.listdir("."):
        if f.endswith(".session"):
            phone = f.replace(".session", "")
            accounts.append({
                "phone": phone,
                "restricted": phone in restricted
            })
    return {"active": active_phone, "accounts": accounts}

@app.post("/api/accounts/delete")
async def api_delete_account(req: AuthPhoneReq):
    global active_phone, client
    target = req.phone
    
    if client and active_phone == target and client.is_connected():
        await client.disconnect()
        client = None
        active_phone = ""
        
    try:
        if os.path.exists(f"{target}.session"):
            os.remove(f"{target}.session")
        if os.path.exists(f"{target}.session-journal"):
            os.remove(f"{target}.session-journal")
        
        # Clean up any pending auth hash
        if target in temp_auth_clients:
            del temp_auth_clients[target]
            
        unmark_restricted(target)
            
        await emit_log(f"[+] Successfully purged account data for {target}.")
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete session file: {str(e)}")

@app.post("/api/accounts/switch")
async def api_switch_account(req: AuthPhoneReq):
    global active_phone, client
    if client and client.is_connected():
        await client.disconnect()
    
    old_phone = active_phone
    active_phone = req.phone
    client = get_base_client(active_phone)
    await client.connect()
    
    if await client.is_user_authorized():
        unmark_restricted(active_phone)
        await emit_log(f"[+] Switched successfully to existing account: {active_phone}")
        return {"status": "switched", "phone": active_phone}
    else:
        # AUTO-REMOVE INCOMPLETE OR BANNED ACCOUNTS ENTIRELY
        await client.disconnect()
        if os.path.exists(f"{active_phone}.session"):
            os.remove(f"{active_phone}.session")
        if os.path.exists(f"{active_phone}.session-journal"):
            os.remove(f"{active_phone}.session-journal")
            
        # Revert memory connection safely
        client = None
        if old_phone and os.path.exists(f"{old_phone}.session"):
            active_phone = old_phone
            client = get_base_client(active_phone)
            await client.connect()
        else:
            active_phone = ""
            
        await emit_log(f"[!] Blocked switch to empty account. Ghost session {req.phone} was auto-deleted.")
        raise HTTPException(status_code=400, detail="Account unlinked/banned. Ghost session auto-deleted.")

@app.post("/api/auth/send-code")
async def api_auth_send_code(req: AuthPhoneReq):
    phone = req.phone
    
    # CRITICAL FIX: Prevent synchronous SQLite file lock freezing the entire event loop
    if client and active_phone == phone and client.is_connected():
        return {"status": "already_authorized"}
        
    temp_client = get_base_client(phone)
    await temp_client.connect()
    
    if await temp_client.is_user_authorized():
        await temp_client.disconnect()
        return {"status": "already_authorized"}
    
    try:
        res = await temp_client.send_code_request(phone)
        temp_auth_clients[phone] = { "client": temp_client, "hash": res.phone_code_hash }
        await emit_log(f"[+] Code successfully sent to {phone}.")
        return {"status": "code_sent", "phone_code_hash": res.phone_code_hash}
    except Exception as e:
        await temp_client.disconnect()
        # AUTO-REMOVE GHOST SESSION IF CODE FAILS TO SEND
        if os.path.exists(f"{phone}.session"):
            os.remove(f"{phone}.session")
        if os.path.exists(f"{phone}.session-journal"):
            os.remove(f"{phone}.session-journal")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/auth/submit-code")
async def api_auth_submit_code(req: AuthCodeReq):
    global active_phone, client
    phone = req.phone
    code = req.code
    
    if phone not in temp_auth_clients:
        raise HTTPException(status_code=400, detail="No active authentication process for this phone number.")
        
    temp = temp_auth_clients[phone]
    temp_client = temp["client"]
    try:
        await temp_client.sign_in(phone=phone, code=code, phone_code_hash=temp["hash"])
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    # Switch to this new client globally
    if client and client.is_connected():
        await client.disconnect()
        
    client = temp_client
    active_phone = phone
    del temp_auth_clients[phone]
    
    await emit_log(f"[+] Authentication successful. Switched to {phone}.")
    return {"status": "success", "phone": phone}

@app.post("/api/scrape")
async def api_scrape(req: ScrapeReq):
    global active_task, current_running_task
    if active_task:
        raise HTTPException(status_code=400, detail="Task already running")
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="No active authorized client")
    active_task = "scrape"
    current_running_task = asyncio.create_task(do_scrape_users(req.url))
    return {"status": "started", "task": "scrape"}

@app.post("/api/add-group")
async def api_add_group(req: TargetReq):
    global active_task, current_running_task
    if active_task:
        raise HTTPException(status_code=400, detail="Task already running")
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="No active authorized client")
    active_task = "add_group"
    current_running_task = asyncio.create_task(do_add_group(req.url))
    return {"status": "started", "task": "add_group"}

@app.post("/api/add-contacts")
async def api_add_contacts():
    global active_task, current_running_task
    if active_task:
        raise HTTPException(status_code=400, detail="Task already running")
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="No active authorized client")
    active_task = "add_contacts"
    current_running_task = asyncio.create_task(do_add_contacts())
    return {"status": "started", "task": "add_contacts"}

@app.post("/api/clear")
async def api_clear(req: ClearReq):
    global active_task
    if active_task:
        raise HTTPException(status_code=400, detail="Task already running")
    
    choice = req.choice
    if choice == '1':
        count = 0
        if os.path.exists("scraped_users.csv"):
            os.remove("scraped_users.csv")
            count += 1
        for file in os.listdir("."):
            if file.startswith("processed_") and file.endswith(".txt"):
                os.remove(file)
                count += 1
        await emit_log(f"[+] Wiped {count} file(s).")
    elif choice == '2':
        if os.path.exists("scraped_users.csv"):
            os.remove("scraped_users.csv")
            await emit_log("[+] Deleted scraped_users.csv")
        else:
            await emit_log("[-] No scraped_users.csv found.")
    elif choice == '3':
        if not os.path.exists("scraped_users.csv"):
            await emit_log("[-] No scraped_users.csv to clean.")
            return {"status": "done"}
        processed_ids = set()
        for file in os.listdir("."):
            if file.startswith("processed_") and file.endswith(".txt"):
                with open(file, "r", encoding="utf-8") as pf:
                    for line in pf:
                        line_id = line.strip()
                        if line_id:
                            processed_ids.add(line_id)
        kept_rows = []
        with open("scraped_users.csv", "r", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=",")
            header = next(reader, None)
            for row in reader:
                if row and len(row) > 1 and str(row[1]).strip() not in processed_ids:
                    kept_rows.append(row)
        with open("scraped_users.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, delimiter=",", lineterminator="\n")
            if header: writer.writerow(header)
            writer.writerows(kept_rows)
        await emit_log("[+] Cleaned scraped_users.csv")
    return {"status": "done"}

@app.post("/api/clear-contacts")
async def api_clear_contacts():
    global active_task, current_running_task
    if active_task:
        raise HTTPException(status_code=400, detail="Task already running")
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="No active authorized client")
    active_task = "clear_contacts"
    current_running_task = asyncio.create_task(do_clear_contacts())
    return {"status": "started", "task": "clear_contacts"}

@app.post("/api/logout")
async def api_logout():
    global active_phone, client
    if client and await client.is_user_authorized():
        await client.log_out()
        await emit_log(f"[+] Successfully logged out {active_phone}.")
        active_phone = ""
    return {"status": "done"}

# Serve React App
if os.path.exists("frontend/dist"):
    app.mount("/", StaticFiles(directory="frontend/dist", html=True), name="frontend")
else:
    @app.get("/")
    async def index():
        return {"msg": "Frontend not built. Run 'npm run build' inside frontend folder."}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=True)
