import logging
import json
import openai
import requests
import yaml
import time
import asyncio
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.const import EVENT_STATE_CHANGED


DOMAIN = "openassist"
EVENT_OPENASSIST_UPDATE = f"{DOMAIN}_update"

_LOGGER = logging.getLogger(__name__)

MODEL = "text-embedding-ada-002"

index_name = "entities"

with open('/config/.storage/core.entity_registry', 'r') as f:
    entities = json.load(f)["data"]["entities"]



def create_embedding(input, model):
    _LOGGER.debug("Creating embedding")
    return openai.Embedding.create(input=input, engine=model)['data'][0]['embedding']

def post_request(url, headers, json_payload):
    _LOGGER.debug("Sending POST request")
    response = requests.post(url, headers=headers, json=json_payload)
    return response.json()


def get_request_pinecone(url, headers):
    _LOGGER.debug("Sending GET request")
    response = requests.get(url, headers=headers)
    return response.json()



def get_request_pinecone_host(url, headers):
    _LOGGER.debug("Sending GET request")
    response = requests.get(url, headers=headers)
    parsed_response = response.json()
    return parsed_response, parsed_response.get('status', {}).get('host')




def post_request_pinecone(url, headers, json_payload):
    _LOGGER.debug(f"Sending POST request to URL: {url}")
    _LOGGER.debug(f"POST data: {json.dumps(json_payload, indent=2)}")  # pretty print the JSON data
    response = requests.post(url, headers=headers, json=json_payload)
    if response.content:  # only attempt to parse if there's a response
        return response.json()
    else:
        return None




async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the OpenAI Assistant component."""
    _LOGGER.debug("Setting up OpenAssist")

    conf = config[DOMAIN] 
    openai.api_key = conf['openai_key']
    pinecone_env = conf['pinecone_env']
    headers = {
        'Api-Key': conf['pinecone_key'],
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }


    async def state_change_handler(event):
        """Handle an OpenAssist state change."""
        entity_id = event.data.get("entity_id")
        if entity_id == "input_text.openassist_prompt":
            _LOGGER.debug("Handling state change event for openassist_prompt")
            new_state = event.data.get("new_state")
            if new_state is not None:
                _LOGGER.debug("Generating embeddings for new state")
                xq = await hass.async_add_executor_job(create_embedding, new_state.state, MODEL)
                _LOGGER.debug("Embeddings generated, preparing payload for Pinecone")

                # Fetch Pinecone host information
                response = await hass.async_add_executor_job(
                    get_request_pinecone,
                    f'https://controller.{pinecone_env}.pinecone.io/databases/entities',
                    headers
                )
                pinecone_host = response.get('status', {}).get('host')
                if not pinecone_host:
                    _LOGGER.error("Unable to fetch Pinecone host information.")
                    return

                url = f"https://{pinecone_host}/query"

                payload = {
                    "vector": list(xq),
                    "includeMetadata": True,
                    "topK": 1
                }
                _LOGGER.debug("Payload prepared, sending POST request to Pinecone")
                response_json = await hass.async_add_executor_job(post_request, url, headers, payload)
                _LOGGER.debug("POST request to Pinecone complete, processing response")
                match = response_json['matches'][0]
                _LOGGER.debug(f"Pinecone match: {match['score']:.2f}: {match['metadata']}")
                _LOGGER.debug("Firing OpenAssist update event")
                hass.bus.async_fire(EVENT_OPENASSIST_UPDATE, {"new_state": new_state.state, "metadata": match['metadata']})
                _LOGGER.debug("OpenAssist update event fired")


    async def state_change_handler_pinecone(event):
        """Handle an OpenAssist state change."""
        entity_id = event.data.get("entity_id")

        if entity_id == "input_text.pinecone_index":
            environment = event.data.get("new_state")
            if environment is not None:
                environment_str = environment.state

                response = await hass.async_add_executor_job(
                    get_request_pinecone,
                    f'https://controller.{environment_str}.pinecone.io/databases',
                    headers  # add headers here
                )

                existing_indexes = response
                hass.states.async_set("sensor.openassist_response", "Building index", {"message": "Please wait while the Pinecone index gets built"})
                
                if index_name not in existing_indexes:
                    # Create Pinecone index if it doesn't exist
                    index_payload = {
                        "name": index_name,
                        "dimension": 1536,
                        "metric": "cosine",
                        "pods": 1,
                        "replicas": 1,
                        "pod_type": "s1.x1"
                    }
                    try:
                        response = await hass.async_add_executor_job(post_request_pinecone, f'https://controller.{environment_str}.pinecone.io/databases', headers, index_payload)
                        if response and 'status_code' in response:
                            if response['status_code'] == 201:  # HTTP Status Code for 'Created'
                                _LOGGER.debug(f"Index '{index_name}' has been created successfully.")
                            else:
                                _LOGGER.debug(f"Failed to create index. HTTP status code: {response['status_code']}. Response: {response['text']}")
                                exit()  # Exit if we couldn't create the index
                    except JSONDecodeError as e:
                        _LOGGER.error(f"Error decoding JSON response: {e}")

                # Wait until the index is ready
                while True:
                    response, host = await hass.async_add_executor_job(
                        get_request_pinecone_host,
                        f'https://controller.{environment_str}.pinecone.io/databases/{index_name}',
                        headers
                    )
                    status = response.get('status', {}).get('state')
                    if status == 'Ready':
                        _LOGGER.debug("Index is ready.")
                        _LOGGER.debug("Waiting an additional 3 minutes before beginning upsert operations...")
                        hass.states.async_set("sensor.openassist_response", "Ready", {"message": "Index created! Data upload will begin in 3 minutes."})
                        await asyncio.sleep(180)
                        break

                    else:
                        _LOGGER.debug("Index is not ready yet, waiting for 5 seconds...")
                        await asyncio.sleep(5)  # use asyncio.sleep instead of time.sleep

                # Pinecone service url (updated to include the host)
                url = f"https://{host}"

                # Populate the index
                hass.states.async_set("sensor.openassist_response", "Upserting data", {"message": "Upserting data. You will be notified once complete."})
                for entity in entities:
                    # Create a string representation of the entity
                    entity_str = json.dumps(entity)
                    # Create the embedding
                    res = await hass.async_add_executor_job(create_embedding, entity_str, MODEL)
                    _LOGGER.debug(f"res: {res}")
                    embed = res


                    # Create a new dictionary with only the fields we want
                    metadata = {field: str(entity[field]) for field in ["device_id", "entity_id", "original_name", "platform", "unique_id", "unit_of_measurement"] if field in entity}

                    # Define the payload to post
                    payload = {
                        "vectors": [
                            {
                                "id": entity["entity_id"],
                                "values": list(embed),
                                "namespace": "entities",
                                "metadata": metadata  # not serializing the metadata
                            }
                        ]
                    }

                    # Make the POST request to Pinecone service
                    response = await hass.async_add_executor_job(post_request_pinecone, f"{url}/vectors/upsert", headers, payload)
                    if response and 'status_code' in response:
                        if response['status_code'] == 200:  # HTTP Status Code for 'OK'
                            _LOGGER.debug(f"Upsert response: {response['text']}")
                        else:
                            _LOGGER.debug(f"Failed to upsert. HTTP status code: {response['status_code']}. Response: {response['text']}")
                hass.states.async_set("sensor.openassist_response", "Ready", {"message": "Your Pinecone index is ready to use! Enjoy."})






    hass.bus.async_listen(EVENT_STATE_CHANGED, state_change_handler)
    hass.bus.async_listen(EVENT_STATE_CHANGED, state_change_handler_pinecone)
    return True
