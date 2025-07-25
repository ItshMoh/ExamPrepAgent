import json
import os
import requests
from dotenv import load_dotenv
load_dotenv()
from audio_processing.whisper_handler import whisper_handler
import logging
from llmclient import client
from database.tidb import tidb_client



API_BASE_URL  = os.getenv('BASE_URL')
API_KEY = os.getenv('API_KEY')
SYSTEM_PROMPT = os.getenv('SYSTEM_PROMPT')

def make_chat_completion_request(messages, tools=None, tool_choice="auto"):
    """Make a direct API request to chat completions endpoint"""
    url = f"{API_BASE_URL}/chat/completions"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}" if API_KEY.strip() else "Bearer dummy"
    }
    
    payload = {
        "model": os.getenv('LLM_MODEL'),
        "messages": messages,
        "temperature": 0.7,
        "tool_choice": "auto" 
    }
    
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    
    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=6000)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"API request failed: {str(e)}")
        print(f"Response text: {response.text if 'response' in locals() else 'No response'}")
        raise Exception(f"API request failed: {str(e)}")

async def get_tools():
    """Get available tools using FastMCP client"""
    try:
        async with client:
            tools_response = await client.list_tools()
            available_functions = []
            
            for tool in tools_response:
                func = {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": {
                            "type": "object",
                            "properties": tool.inputSchema.get("properties", {}),
                            "required": tool.inputSchema.get("required", []),
                        },
                    },
                }
                available_functions.append(func)
            
            return available_functions
    except Exception as e:
        print(f"Error getting tools: {str(e)}")
        return []

async def handle_tool_calls(tool_calls):
    """Handle tool calls using FastMCP client"""
    tool_responses = []
    
    try:
        async with client:
            for tool_call in tool_calls:
                function_name = tool_call["function"]["name"]
                function_args = json.loads(tool_call["function"]["arguments"]) if isinstance(tool_call["function"]["arguments"], str) else tool_call["function"]["arguments"]
                
                print(f"Calling tool: {function_name} with args: {function_args}")
                
                # Call tool using FastMCP client
                tool_result = await client.call_tool(name=function_name, arguments=function_args)
                
                result_text = ""
                
                if hasattr(tool_result, 'content') and tool_result.content:
                    for content in tool_result.content:
                        if hasattr(content, 'text'):
                            result_text += content.text
                elif hasattr(tool_result, 'structured_content') and tool_result.structured_content:
                    result_text = json.dumps(tool_result.structured_content)
                else:
                    result_text = "No result"
                
                tool_responses.append({
                    "tool_call_id": tool_call["id"],
                    "role": "tool",
                    "name": function_name,
                    "content": result_text
                })
        
        return tool_responses
    except Exception as e:
        print(f"Error handling tool calls: {str(e)}")
        return []

def format_context_for_llm(context):
    """Convert context list to formatted string for LLM"""
    if not context or (len(context) == 1 and not context[0]["user_query"]):
        return "No previous conversation history."
    
    formatted_context = "Previous conversation history:\n"
    for entry in context:
        if entry["user_query"]:
            formatted_context += f"User: {entry['user_query']}\n"
        if entry["agent_response"]:
            formatted_context += f"Assistant: {entry['agent_response']}\n"
        if entry["tool_response"]:
            formatted_context += f"Tool Result: {entry['tool_response']}\n"
        formatted_context += "\n"
    
    return formatted_context

async def process_message(session_id, user_input):
    """Process a user message and return the response"""
    logger = logging.getLogger(__name__)
    logger.info(f"session id {session_id}")
    available_functions = await get_tools()
    
    # Get session context
    session_context = tidb_client.get_session_context(session_id)
    logger.info(f"type of session_context: {type(session_context)}")
    logger.info(f"Session context: {session_context}")
    context_string = format_context_for_llm(session_context)
    
    messages = [
        {"role": "system", "content": f"""{SYSTEM_PROMPT}
{context_string}"""}
    ]
    
    # Add context messages
    # for entry in session_context:
    #     if entry.get("user_query"):
    #         messages.append({"role": "user", "content": entry["user_query"]})
    #     if entry.get("agent_response"):
    #         messages.append({"role": "assistant", "content": entry["agent_response"]})
    
    # Add current user message
    messages.append({"role": "user", "content": user_input})
    
    # Make initial API request
    completion_response = make_chat_completion_request(
        messages=messages,
        tools=available_functions,
        tool_choice="auto"
    )
    
    assistant_message = completion_response["choices"][0]["message"]
    tool_response_content = ""
    
    if assistant_message.get("tool_calls"):
        tool_responses = await handle_tool_calls(assistant_message["tool_calls"])
        tool_response_content = json.dumps([resp["content"] for resp in tool_responses])
        
        messages.append({
            "role": "assistant",
            "content": assistant_message.get("content"),
            "tool_calls": assistant_message["tool_calls"]
        })
        
        # Add tool responses
        for tool_response in tool_responses:
            messages.append(tool_response)
        
        final_completion_response = make_chat_completion_request(
            messages=messages,
            tools=available_functions,
            tool_choice="auto"
        )
        
        final_message = final_completion_response["choices"][0]["message"]
        response_text = final_message["content"]
    else:
        response_text = assistant_message["content"]
    
    # Update session context
    new_context_entry = {
        "user_query": user_input,
        "agent_response": response_text,
        "tool_response": tool_response_content
    }
    
    session_context.append(new_context_entry)
    tidb_client.update_session_context(session_id, session_context)
    
    return response_text

async def process_audio_message(session_id, audio_data_wav, filename_wav, available_functions, language=None):
    """Process an audio message and return the response"""
    logger = logging.getLogger(__name__)
    
    try:
        if not audio_data_wav:
            return { "success": False, "error": "No audio data received for processing", "transcription": "", "response": ""}

        logger.info(f"Starting transcription for WAV data (filename: {filename_wav}) for session {session_id}")
        transcription_result = whisper_handler.transcribe_audio_bytes(audio_data_wav, filename_wav, language)
        
        if not transcription_result["success"]:
            return {
                "success": False,
                "error": f"Transcription failed: {transcription_result['error']}",
                "transcription": "",
                "response": ""
            }

        transcribed_text = transcription_result["text"]
        detected_language = transcription_result["language"] 
        
        logger.info(f"Transcription successful: '{transcribed_text[:100]}...' (Language: {detected_language})")

        # Process the transcribed text through the normal message pipeline
        if transcribed_text.strip():
            response_text = await process_message(session_id, transcribed_text)
            
            return {
                "success": True,
                "transcription": transcribed_text,
                "detected_language": detected_language,
                "response": response_text,
                "error": None
            }
        else:
            logger.info("Transcription resulted in empty text (possibly silence).")
            return {
                "success": True, # Transcription itself didn't fail, just no speech
                "transcription": "",
                "detected_language": detected_language,
                "response": "I didn't detect any speech in your audio. Could you please try again?",
                "error": "No speech detected"
            }

    except Exception as e:
        logger.error(f"Audio message processing failed in llm_api: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Internal error during audio processing: {str(e)}",
            "transcription": "",
            "response": ""
        }

def cleanup_server():
    """Cleanup function (no longer needed with FastMCP client)"""
    pass