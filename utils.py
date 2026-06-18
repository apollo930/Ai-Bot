import random, requests, json, os, re, urllib.parse
from dotenv import load_dotenv
load_dotenv()

def get_cpu_temp() -> str:
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        temp = int(f.read().strip()) / 1000
    return f"{temp:.1f}°C"

def get_quote():
	response = requests.get("https://zenquotes.io/api/random")
	json_data = json.loads(response.text)
	quote = "`" + json_data[0]['q'] + "`" + " -" + json_data[0]['a']
	return quote

def transform_instagram_links(text: str) -> tuple[str, bool] | None:
    TRACKING_PARAMS = {"igshid", "igsh", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "fbclid"}
    pattern = r'https?://(?:www\.|m\.|l\.|)?instagram\.com/[a-zA-Z0-9_/?=&%-]+'
    match = re.search(pattern, text)
    if not match:
        return None
    url = match.group().rstrip("|!.,)>")
    is_spoiler = match.start() >= 2 and text[match.start() - 2 : match.start()] == "||" and text[match.end() : match.end() + 2] == "||"
    parsed = urllib.parse.urlparse(url)
    clean_query = "&".join(
        f"{k}={v}" for k, v in urllib.parse.parse_qsl(parsed.query) if k not in TRACKING_PARAMS
    )
    clean = parsed._replace(
        netloc=parsed.netloc.replace("instagram.com", "kkinstagram.com"),
        query=clean_query
    )
    return (urllib.parse.urlunparse(clean), is_spoiler)

def get_response(message:str) -> str:
	p_message = message.lower()

	if p_message.split()[0] in ["hello", "hey", "hi", "yo", "sup", "wassup", "hola"]:
		return "_Hello, there!_"
	
	elif p_message.split()[0] in  ["inspire", "motivate"] or "depressed" in p_message.split():
		return get_quote()
	
	else:
		return "Uhh, dk what you're saying, bruh."