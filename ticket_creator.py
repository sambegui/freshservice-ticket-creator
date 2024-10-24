import os
import sys
import json
import logging
import asyncio
import aiohttp
from typing import List, Dict, Any, Optional, Union
from dataclasses import dataclass
from dotenv import load_dotenv
from pick import pick
from rich import print
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from email_validator import validate_email
from logging.handlers import RotatingFileHandler
from urllib.parse import quote

# Constants
API_VERSION = "v2"
CONTENT_TYPE = "application/json"
MAX_RETRIES = 3
RETRY_DELAY = 2

# Load environment variables
load_dotenv()

# Configuration validation
FRESHSERVICE_DOMAIN = os.getenv("FRESHSERVICE_DOMAIN")
API_KEY = os.getenv("API_KEY")

if not FRESHSERVICE_DOMAIN or not API_KEY:
    print("[red]Error: Please set FRESHSERVICE_DOMAIN and API_KEY in your .env file.[/red]")
    sys.exit(1)

BASE_URL = f"https://{FRESHSERVICE_DOMAIN}/api/{API_VERSION}"

# Update the logging configuration
logging.basicConfig(
    level=logging.DEBUG,  # Set to DEBUG to capture detailed logs
    format="%(asctime)s %(levelname)s:%(message)s",
    handlers=[
        RotatingFileHandler(
            "freshservice.log",
            maxBytes=1024*1024,  # 1MB
            backupCount=5
        )
    ]
)

console = Console()

@dataclass
class Workspace:
    id: int
    name: str

class APIError(Exception):
    pass

async def make_request(session: aiohttp.ClientSession, method: str, url: str, **kwargs) -> Dict:
    auth = aiohttp.BasicAuth(API_KEY, 'X')
    if 'headers' not in kwargs:
        kwargs['headers'] = {"Content-Type": CONTENT_TYPE, "Accept": CONTENT_TYPE}
    kwargs['auth'] = auth

    for attempt in range(MAX_RETRIES):
        try:
            async with session.request(method, url, **kwargs) as response:
                response_text = await response.text()
                logging.debug(f"API Response ({response.status}): {response_text}")
                
                if response.status == 404:
                    logging.error(f"API Error {response.status}: {response_text}")
                    raise APIError("Resource not found. Please verify your Freshservice domain and endpoint configuration.")
                elif response.status == 401:
                    logging.error(f"API Error {response.status}: {response_text}")
                    raise APIError("Authentication failed. Please check your API key.")
                elif response.status >= 400:
                    logging.error(f"API Error {response.status}: {response_text}")
                    raise APIError(f"API Error {response.status}: {response_text}")

                try:
                    return await response.json()
                except json.JSONDecodeError:
                    logging.error(f"Failed to parse JSON response: {response_text}")
                    raise APIError("Invalid JSON response from API")

        except aiohttp.ClientError as e:
            if attempt == MAX_RETRIES - 1:
                raise APIError(f"Request failed after {MAX_RETRIES} attempts: {str(e)}")
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))

async def get_workspaces() -> List[Workspace]:
    url = f"{BASE_URL}/workspaces"

    async with aiohttp.ClientSession() as session:
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}")
            ) as progress:
                progress.add_task(description="Fetching workspaces...", total=None)
                data = await make_request(session, 'GET', url)

                if not data or 'workspaces' not in data:
                    raise APIError("No workspaces data found in response")

                workspaces = data['workspaces']
                return [Workspace(id=ws['id'], name=ws['name']) for ws in workspaces]
        except Exception as e:
            logging.error(f"Failed to fetch workspaces: {e}")
            raise APIError(f"Failed to fetch workspaces: {str(e)}")

async def fetch_ticket_fields() -> List[Dict[str, Any]]:
    url = f"{BASE_URL}/ticket_fields"
    auth = aiohttp.BasicAuth(API_KEY, 'X')
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    async with aiohttp.ClientSession(auth=auth) as session:
        data = await make_request(session, 'GET', url, headers=headers)
        return data.get('ticket_fields', [])
    
    with open('ticket_fields_debug.json', 'w') as f:
        json.dump(data, f, indent=4)
    return data.get('ticket_fields', [])

def extract_choices(ticket_fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    for field in ticket_fields:
        if field.get('name') == 'category':
            return field.get('choices', {})
    return {}

def build_category_structure(choices_data: Dict[str, Dict]) -> Dict[str, Any]:
    # Directly use the nested structure from choices_data
    return choices_data

def traverse_category(category_tree: Dict[str, Any]) -> Dict[str, Any]:
    path = {}
    
    # Select category
    category_options = list(category_tree.keys())
    selection, _ = pick(category_options, "Select category:")
    selected_category_name = selection
    selected_category = category_tree[selected_category_name]
    path['category_name'] = selected_category_name

    # Check for sub_categories
    if selected_category and isinstance(selected_category, dict):
        sub_category_options = list(selected_category.keys())
        if sub_category_options:
            selection, _ = pick(sub_category_options, f"Select sub-category for '{selected_category_name}':")
            selected_sub_category_name = selection
            selected_sub_category = selected_category[selected_sub_category_name]
            path['sub_category_name'] = selected_sub_category_name

            # Check for item_categories
            if selected_sub_category and isinstance(selected_sub_category, list) and selected_sub_category:
                selection, _ = pick(selected_sub_category, f"Select item category for '{selected_sub_category_name}':")
                path['item_category_name'] = selection

    return path

async def create_ticket_async(
    first_name: str,
    last_name: str,
    email: str,
    description: str,
    category: str,
    sub_category: str,
    item_category: Optional[str],
    priority: int,
    workspace_id: int,
    attachments: Optional[List[str]] = None
) -> Dict[str, Any]:
    summary = f"Request for {first_name} {last_name}: " + (description[:30] + "..." if len(description) > 30 else description)

    ticket_data = {
        "email": email,
        "subject": summary,
        "description": description,
        "status": 2,  # 2 represents "Open" status in Freshservice
        "priority": priority,
        "category": category,
        "sub_category": sub_category,
        "item_category": item_category,
        "workspace_id": workspace_id,
        "source": 2  # 2 represents "Portal" source
    }

    logging.debug(f"Ticket data being sent: {json.dumps(ticket_data, indent=2)}")

    auth = aiohttp.BasicAuth(API_KEY, 'X')
    url = f"{BASE_URL}/tickets"

    async with aiohttp.ClientSession(auth=auth) as session:
        try:
            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
                progress.add_task(description="Creating ticket...", total=None)

                if attachments:
                    data = aiohttp.FormData()
                    data.add_field('input_data', json.dumps(ticket_data))
                    for file_path in attachments:
                        if os.path.isfile(file_path):
                            data.add_field('attachments[]',
                                        open(file_path, 'rb'),
                                        filename=os.path.basename(file_path))
                        else:
                            console.print(f"[yellow]Warning: Attachment {file_path} not found.[/yellow]")
                    return await make_request(session, 'POST', url, data=data)
                else:
                    headers = {'Content-Type': 'application/json'}
                    return await make_request(session, 'POST', url, headers=headers, json=ticket_data)

        except Exception as e:
            logging.error(f"Failed to create ticket: {e}")
            raise

def validate_user_input(prompt: str, validation_func) -> Any:
    while True:
        try:
            value = input(prompt)
            return validation_func(value)
        except Exception as e:
            console.print(f"[red]{e}[/red]")
    
async def get_user_info(email: str) -> Dict[str, str]:
    """Fetch user info from Freshservice using email - tries both requesters and agents endpoints"""
    async with aiohttp.ClientSession() as session:
        # First try requesters endpoint
        requester_url = f"{BASE_URL}/requesters?email={quote(email)}"
        agent_url = f"{BASE_URL}/agents?email={quote(email)}"
        
        logging.debug(f"Attempting requester lookup with URL: {requester_url}")
        
        try:
            # Try requesters endpoint first
            auth = aiohttp.BasicAuth(API_KEY, 'X')
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json"
            }

            data = await make_request(session, 'GET', requester_url, headers=headers)
            logging.debug(f"Requester lookup response: {json.dumps(data, indent=2)}")

            if data and isinstance(data, dict) and 'requesters' in data and data['requesters']:
                user = data['requesters'][0]
                return {
                    'first_name': user.get('first_name', ''),
                    'last_name': user.get('last_name', ''),
                    'email': email,
                    'id': user.get('id')
                }

            # If no requester found, try agents endpoint
            logging.debug(f"No requester found, trying agents endpoint: {agent_url}")
            data = await make_request(session, 'GET', agent_url, headers=headers)
            logging.debug(f"Agent lookup response: {json.dumps(data, indent=2)}")

            if data and isinstance(data, dict) and 'agents' in data and data['agents']:
                user = data['agents'][0]
                return {
                    'first_name': user.get('first_name', ''),
                    'last_name': user.get('last_name', ''),
                    'email': email,
                    'id': user.get('id')
                }

            console.print(f"[yellow]Warning: No user found for email {email} in either requesters or agents[/yellow]")
            return None

        except Exception as e:
            logging.error(f"Error fetching user info: {str(e)}")
            console.print(f"[red]Error looking up user: {str(e)}[/red]")
            return None
    
    console.print(f"[yellow]Warning: No user found for email {email}[/yellow]")
    logging.error(f"Failed to find user with email {email} after trying multiple endpoints")
    return None

async def main_async():
    try:
        # Verify configuration
        if not FRESHSERVICE_DOMAIN.endswith('freshservice.com'):
            raise APIError("Invalid Freshservice domain format. Should end with 'freshservice.com'")

        if len(API_KEY) < 10:
            raise APIError("API key appears to be invalid or too short")

        # Get user email and info
        email = validate_user_input("Enter requester's email address: ", lambda x: validate_email(x).email)
        user_info = await get_user_info(email)
        
        if not user_info:
            console.print("[red]Unable to find user in Freshservice. Please verify the email address.[/red]")
            sys.exit(1)
            
        first_name = user_info['first_name']
        last_name = user_info['last_name']
        
        console.print(f"[green]Creating ticket for: {first_name} {last_name} ({email})[/green]")

        description = input("Enter ticket description: ").strip()

        workspaces = await get_workspaces()
        if not workspaces:
            console.print("[red]No workspaces found![/red]")
            sys.exit(1)

        workspace_options = [f"{ws.name} (ID: {ws.id})" for ws in workspaces]
        workspace_selection, _ = pick(workspace_options, "Select workspace:")
        selected_workspace_id = int(workspace_selection.split(" (ID: ")[-1].rstrip(")"))

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
            progress.add_task(description="Fetching and updating category choices...", total=None)
            ticket_fields = await fetch_ticket_fields()
            choices_data = extract_choices(ticket_fields)
            with open('choices_structure.json', 'w') as f:
                json.dump(choices_data, f, indent=4)
            logging.debug("Choices structure saved to 'choices_structure.json'.")

        category_structure = build_category_structure(choices_data)

        category_path = traverse_category(category_structure)
        if not category_path:
            console.print("[red]No category selected![/red]")
            sys.exit(1)
        logging.debug(f"Selected category path: {category_path}")

        priority_options = ["Low", "Medium", "High", "Urgent"]
        priority_selection, _ = pick(priority_options, "Select priority:")
        priority = priority_options.index(priority_selection) + 1

        attachments = []
        if input("Do you want to attach files? (y/N): ").strip().lower() == 'y':
            console.print("[yellow]Please enter file paths separated by commas, without quotes.[/yellow]")
            file_paths = input("Enter file paths separated by commas: ").split(',')
            attachments = [path.strip() for path in file_paths]

        result = await create_ticket_async(
            first_name=first_name, 
            last_name=last_name, 
            email=email, 
            description=description,
            category=category_path['category_name'],
            sub_category=category_path.get('sub_category_name', ''),
            item_category=category_path.get('item_category_name', ''),
            priority=priority,
            workspace_id=selected_workspace_id,
            attachments=attachments
        )

        if "ticket" in result:
            console.print(f"[green]Ticket created successfully! Ticket ID: {result['ticket']['id']}[/green]")
        else:
            console.print(f"[red]Error creating ticket: {result}[/red]")

    except KeyboardInterrupt:
        console.print("\n[red]Operation cancelled by user.[/red]")
        sys.exit(0)
    except APIError as e:
        console.print(f"[red]API Error: {str(e)}[/red]")
        logging.error(f"API Error: {str(e)}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Unexpected Error: {e}")
        console.print(f"[red]Unexpected Error: {e}[/red]")
        sys.exit(1)

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()