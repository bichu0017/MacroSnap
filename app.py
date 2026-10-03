import json
import os
import time
import tomllib
from pathlib import Path

import streamlit as st
from google import genai
from google.genai import types
from twilio.rest import Client as TwilioClient

from prompts import SUMMARY_REQUEST_PROMPT, SYSTEM_PROMPT, WELCOME_MESSAGE_TEMPLATE

MODEL_NAME = "gemini-3.8-flash"
st.set_page_config(page_title="MacroSnap", page_icon="🥗")


def get_secret(name):
	try:
		value = st.secrets.get(name)
		if value:
			return value
	except Exception:
		pass

	value = os.getenv(name)
	if value:
		return value

	legacy_secrets = Path(__file__).parent / "stremelit" / "secret.toml"
	try:
		raw_secrets = legacy_secrets.read_text(encoding="utf-8")
		try:
			secrets = tomllib.loads(raw_secrets)
		except tomllib.TOMLDecodeError:
			normalized_lines = []
			for line in raw_secrets.splitlines():
				if "=" in line and not line.lstrip().startswith("#"):
					key, value = line.split("=", 1)
					value = value.strip()
					if value[:1] in ('"', "'"):
						value = value[1:]
					if value[-1:] in ('"', "'"):
						value = value[:-1]
					line = f"{key} = {json.dumps(value)}"
				normalized_lines.append(line)
			secrets = tomllib.loads("\n".join(normalized_lines))
		return secrets.get(name)
	except (OSError, tomllib.TOMLDecodeError):
		return None


@st.cache_resource
def get_gemini_client(api_key):
	return genai.Client(api_key=api_key)


@st.cache_resource
def get_twilio_client(account_sid, auth_token):
	return TwilioClient(account_sid, auth_token)


def render_message(message):
	with st.chat_message(message["role"]):
		if message["kind"] == "text":
			st.write(message["content"])
		elif message["kind"] == "image":
			st.image(message["content"])


def add_message(role, kind, content):
	message = {"role": role, "kind": kind, "content": content}
	st.session_state.messages.append(message)
	render_message(message)


def ask_gemini(parts):
	for attempt in range(3):
		try:
			response = st.session_state.chat.send_message(parts)
			return response.text or "I couldn't estimate that meal. Please try again."
		except Exception as error:
			error_text = str(error).lower()
			is_dns_failure = any(
				message in error_text
				for message in (
					"getaddrinfo failed",
					"temporary failure in name resolution",
					"name or service not known",
				)
			)
			if is_dns_failure:
				return (
					"Can't reach Gemini because this device can't resolve internet addresses. "
					"Check your internet connection, DNS settings, VPN, or proxy, then try again."
				)

			status_code = getattr(error, "code", None) or getattr(error, "status_code", None)
			is_overloaded = str(status_code) == "503" or "503 UNAVAILABLE" in str(error).upper()
			if not is_overloaded:
				return f"Sorry, something went wrong: {error}"
			if attempt < 2:
				time.sleep(2 ** (attempt + 1))

	return (
		"Gemini is still unavailable after several attempts. Please try again in a minute. "
		"If this keeps happening, check your Gemini API status and quota."
	)


def clean_whatsapp_text(text):
	if not text:
		return "No nutrition summary available."
	text = " ".join(text.split())
	return text[:1500] + "..." if len(text) > 1500 else text


def send_whatsapp(to_number, user_name, summary):
	account_sid = get_secret("TWILIO_ACCOUNT_SID")
	auth_token = get_secret("TWILIO_AUTH_TOKEN")
	sender = get_secret("TWILIO_WHATSAPP_FROM")
	content_sid = get_secret("TWILIO_CONTENT_SID") or get_secret("TWILIO_CONTENT")
	if not all((account_sid, auth_token, sender, content_sid)):
		return False, "Configure the Twilio account, WhatsApp sender, and content template in Streamlit secrets."

	try:
		twilio_client = get_twilio_client(account_sid, auth_token)
		content_variables = json.dumps(
			{"1": user_name, "2": clean_whatsapp_text(summary)},
			ensure_ascii=False,
		)
		recipient = to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}"
		message = twilio_client.messages.create(
			from_=sender,
			to=recipient,
			content_sid=content_sid,
			content_variables=content_variables,
		)
		return True, message.sid
	except Exception as error:
		return False, str(error)


gemini_api_key = get_secret("GEMINI_API_KEY")

if "onboarded" not in st.session_state:
	st.title("🥗 MacroSnap")
	st.caption("Snap it. Track it. Text yourself the results.")
	with st.form("onboarding_form"):
		name = st.text_input("Your name")
		whatsapp_number = st.text_input(
			"WhatsApp number (with country code)",
			placeholder="+91XXXXXXXXXX",
			help="This is the number MacroSnap will text your summary to.",
		)
		submitted = st.form_submit_button("Let's go 🚀")

	if submitted:
		if not name.strip() or not whatsapp_number.strip():
			st.warning("Please fill in both your name and WhatsApp number.")
		elif not gemini_api_key:
			st.error(
				"Set GEMINI_API_KEY in a valid .streamlit/secrets.toml file "
				"(see .streamlit/secrets.toml.example), then try again."
			)
		else:
			st.session_state.name = name.strip()
			st.session_state.whatsapp_number = whatsapp_number.strip()
			gemini_client = get_gemini_client(gemini_api_key)
			st.session_state.chat = gemini_client.chats.create(
				model=MODEL_NAME,
				config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
			)
			st.session_state.messages = []
			st.session_state.onboarded = True
			st.rerun()
	st.stop()


header_col, button_col = st.columns([5, 2], vertical_alignment="center")
with header_col:
	st.title("🥗 MacroSnap")
with button_col:
	send_disabled = len(st.session_state.messages) <= 2
	if st.button("📤 Send to WhatsApp", disabled=send_disabled, use_container_width=True):
		with st.spinner("Summarizing your meals..."):
			summary = ask_gemini([SUMMARY_REQUEST_PROMPT])
		success, info = send_whatsapp(
			st.session_state.whatsapp_number,
			st.session_state.name,
			summary,
		)
		if success:
			st.success("Sent! Check your WhatsApp.")
		else:
			st.error(f"Couldn't send that: {info}")

st.caption(
	f"Logged in as {st.session_state.name} · updates go to {st.session_state.whatsapp_number}"
)

if not st.session_state.messages:
	add_message(
		"assistant",
		"text",
		WELCOME_MESSAGE_TEMPLATE.format(name=st.session_state.name),
	)
else:
	for message in st.session_state.messages:
		render_message(message)

user_input = st.chat_input(
	"Ask a question, or attach a photo of your meal",
	accept_file=True,
	file_type=["jpg", "jpeg", "png"],
)

if user_input:
	photo = user_input.files[0] if user_input.files else None
	text = user_input.text.strip()
	parts = []

	if photo is not None:
		photo_bytes = photo.getvalue()
		add_message("user", "image", photo_bytes)
		parts.append(types.Part.from_bytes(data=photo_bytes, mime_type=photo.type))
	if text:
		add_message("user", "text", text)
		parts.append(text)
	elif photo is not None:
		parts.append("What is this meal? Give me the calories and macros.")

	if parts:
		with st.spinner("Crunching the numbers..."):
			answer = ask_gemini(parts)
		add_message("assistant", "text", answer)