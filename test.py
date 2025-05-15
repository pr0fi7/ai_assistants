
from google import genai
import os, json, dotenv
from config import SYSTEM_PROMPT, validation_json_schema, state_json_schema, VALIDATION_PROMPT, SIMPLE_STATE_PROMPT
import re 

dotenv.load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


# 2) Define your JSON‐output template
response_template = {
    "role":      "<assistant|user>",
    "content":   "<string>"
}

def get_gemini_response(conversation: list[dict], json_schema = None) -> dict:
    prompt = f"""
    Вот история чата (в JSON):
    {json.dumps(conversation, ensure_ascii=False, indent=2)}
    """
    if json_schema:
        prompt += f"""
        Вот JSON-схема, которую нужно использовать для ответа:
        {json.dumps(json_schema, ensure_ascii=False, indent=2)}
        """
    else:
        prompt += f"""
        Вот JSON-схема, которую нужно использовать для ответа:
        {json.dumps(response_template, ensure_ascii=False, indent=2)}
        """

    response = client.models.generate_content(
        model="gemini-2.5-flash-preview-04-17",
        contents=prompt
    )
    return response.text.strip()



def verify_response(conversation, candidate) -> str:
    system_content = VALIDATION_PROMPT.format(
    message=candidate,
    conversation=conversation)

    # print(f"System: {system_content}")
    response = get_gemini_response(
        conversation=[
            {"role": "system", "content": system_content},
        ],
        json_schema=validation_json_schema,
    )
    match = re.search(r'({.*})', response, re.DOTALL)
    if not match:
        raise ValueError("Couldn't find a JSON object in the response")
    json_str = match.group(1)
    resp_json = json.loads(json_str)
    answer = resp_json.get("answer", "")
    possible_response = resp_json.get("possible_response", "").strip()
    return answer, possible_response



def state_agent_response(conversation: list[dict], user_prompt: str) -> str:
    system_content = SIMPLE_STATE_PROMPT.format(
        message=user_prompt,
        conversation=conversation_to_string(conversation))

    if len(conversation) > 15:
        print("there are more than 15 messages in the conversation")
        response = get_gemini_response(
            conversation=[
                {"role": "system", "content": system_content},
            ],
            json_schema=state_json_schema,
        )
        match = re.search(r'({.*})', response, re.DOTALL)
        if not match:
            raise ValueError("Couldn't find a JSON object in the response")
        json_str = match.group(1)
        resp_json = json.loads(json_str)
        
        verdict = resp_json.get("verdict", "")
        if verdict == True:
            return resp_json.get("updated_answer", "").strip()
        else:
            return False
    else:
        False

def get_last_n_messages(conversation: list[dict], n: int) -> list[dict]:
    return conversation[-n:] if len(conversation) > n else conversation

def conversation_to_string(conversation: list[dict]) -> str:
    return "\n".join([f"{msg['role']}: {msg['content']}" for msg in conversation])

def multi_agent_chat(user_prompt: str, conversation: list[dict] | None, phone_number, max_rounds: int = 5) -> list[dict]:
    if conversation is None:
        conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    conversation.append({"role": "user", "content": user_prompt})

    response = get_gemini_response(conversation)
    match = re.search(r'({.*})', response, re.DOTALL)
    if not match:
        raise ValueError("Couldn't find a JSON object in the response")
    json_str = match.group(1)
    resp_json = json.loads(json_str)
    print("Gemini response:", response)
    resp = resp_json.get("content", "").strip()
    
    print("Assistant:", resp)

    for round_idx in range(max_rounds):
        last_n = get_last_n_messages(conversation, 10)
        convo_str = conversation_to_string(last_n)

        answer, possible_response = verify_response(conversation, resp)
        if answer != True:
            print("Assistant (revised):", possible_response)
        if answer == True:
            break

    if not phone_number:
        state_response = state_agent_response(conversation, resp)
        if state_response:
            print("State Agent:", state_response)
            conversation.append({"role": "assistant", "content": state_response})
            phone_number = True

            return conversation, phone_number
   
    conversation.append({"role": "assistant", "content": resp})

    return conversation, phone_number


if __name__ == "__main__":
    conversation = None
    phone_number = False

    while True:
        user_prompt = input("You: ")
        if user_prompt.strip().lower() == "exit":
            print("Goodbye!")
            break

        conversation, phone_number = multi_agent_chat(user_prompt, conversation, phone_number=phone_number)

