```python
import requests
from bs4 import BeautifulSoup
import logging
import time
import re
from urllib.parse import urljoin, urlparse, parse_qs, unquote
import base64
import json
import hashlib
import random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import os

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# HTTP headers for scraping
headers = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36'
}

# Constants for WordPress
WP_URL = "https://mauritius.mimusjobs.com/wp-json/wp/v2/job-listings"
WP_COMPANY_URL = "https://mauritius.mimusjobs.com/wp-json/wp/v2/company"
WP_MEDIA_URL = "https://mauritius.mimusjobs.com/wp-json/wp/v2/media"
WP_JOB_TYPE_URL = "https://mauritius.mimusjobs.com/wp-json/wp/v2/job_listing_type"
WP_JOB_REGION_URL = "https://mauritius.mimusjobs.com/wp-json/wp/v2/job_listing_region"
WP_USERNAME = "mary"
WP_APP_PASSWORD = "Piab Mwog pfiq pdfK BOGH hDEy"
PROCESSED_IDS_FILE = "mauritius_processed_job_ids.csv"
LAST_PAGE_FILE = "last_processed_page.txt"
JOB_TYPE_MAPPING = {
    "Full-time": "full-time",
    "Part-time": "part-time",
    "Contract": "contract",
    "Temporary": "temporary",
    "Freelance": "freelance",
    "Internship": "internship",
    "Volunteer": "volunteer"
}

FRENCH_TO_ENGLISH_JOB_TYPE = {
    "Temps plein": "Full-time",
    "Temps partiel": "Part-time",
    "Contrat": "Contract",
    "Temporaire": "Temporary",
    "Indépendant": "Freelance",
    "Stage": "Internship",
    "Bénévolat": "Volunteer"
}

def sanitize_text(text, is_url=False):
    if not text:
        return ''
    if is_url:
        text = text.strip()
        if not text.startswith(('http://', 'https://')):
            text = 'https://' + text
        return text
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'(\w)\.(\w)', r'\1. \2', text)
    text = re.sub(r'(\w)(\w)', r'\1 \2', text) if re.match(r'^\w+$', text) else text
    return ' '.join(text.split())

def normalize_for_deduplication(text):
    """Normalize text for deduplication by removing spaces, punctuation, and converting to lowercase."""
    text = re.sub(r'[^\w\s]', '', text)  # Remove punctuation
    text = re.sub(r'\s+', '', text)      # Remove all whitespace
    return text.lower()

def generate_job_id(job_title, company_name):
    """Generate a unique job ID based on job title and company name."""
    combined = f"{job_title}_{company_name}"
    return hashlib.md5(combined.encode()).hexdigest()[:16]

def split_paragraphs(text, max_length=200):
    """Split large paragraphs into smaller ones, each up to max_length characters."""
    paragraphs = text.split('\n\n')
    result = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        while len(para) > max_length:
            split_point = para.rfind(' ', 0, max_length)
            if split_point == -1:
                split_point = para.rfind('.', 0, max_length)
            if split_point == -1:
                split_point = max_length
            result.append(para[:split_point].strip())
            para = para[split_point:].strip()
        if para:
            result.append(para)
    return '\n\n'.join(result)

def get_or_create_term(term_name, taxonomy, wp_url, auth_headers):
    term_name = sanitize_text(term_name)
    if not term_name:
        return None
    check_url = f"{wp_url}?search={term_name}"
    try:
        response = requests.get(check_url, headers=auth_headers, timeout=10, verify=False)
        response.raise_for_status()
        terms = response.json()
        for term in terms:
            if term['name'].lower() == term_name.lower():
                return term['id']
        post_data = {"name": term_name, "slug": term_name.lower().replace(' ', '-')}
        response = requests.post(wp_url, json=post_data, headers=auth_headers, timeout=10, verify=False)
        response.raise_for_status()
        term = response.json()
        logger.info(f"Created new {taxonomy} term: {term_name}, ID: {term['id']}")
        return term['id']
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to get or create {taxonomy} term {term_name}: {str(e)}")
        return None

def check_existing_job(job_title, company_name, auth_headers):
    """Check if a job with the same title and company already exists on WordPress."""
    check_url = f"{WP_URL}?search={job_title}&meta_key=_company_name&meta_value={company_name}"
    try:
        response = requests.get(check_url, headers=auth_headers, timeout=10, verify=False)
        response.raise_for_status()
        posts = response.json()
        if posts:
            logger.info(f"Found existing job on WordPress: {job_title} at {company_name}, Post ID: {posts[0].get('id')}")
            return posts[0].get('id'), posts[0].get('link')
        return None, None
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to check existing job {job_title} at {company_name}: {str(e)}")
        return None, None

def save_company_to_wordpress(index, company_data, wp_headers):
    company_name = company_data.get("company_name", "")
    company_details = company_data.get("company_details", "")
    company_logo = company_data.get("company_logo", "")
    company_website = company_data.get("company_website_url", "")
    company_industry = company_data.get("company_industry", "")
    company_founded = company_data.get("company_founded", "")
    company_type = company_data.get("company_type", "")
    company_address = company_data.get("company_address", "")
    
    # Check if company already exists
    check_url = f"{WP_COMPANY_URL}?search={company_name}"
    try:
        response = requests.get(check_url, headers=wp_headers, timeout=10, verify=False)
        response.raise_for_status()
        posts = response.json()
        if posts:
            post = posts[0]
            logger.info(f"Found existing company {company_name}: Post ID {post.get('id')}, URL {post.get('link')}")
            return post.get("id"), post.get("link")
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to check existing company {company_name}: {str(e)}")

    attachment_id = 0
    if company_logo:
        try:
            logo_response = requests.get(company_logo, headers=headers, timeout=10)
            logo_response.raise_for_status()
            logo_headers = {
                "Authorization": wp_headers["Authorization"],
                "Content-Disposition": f'attachment; filename="{company_name}_logo.jpg"',
                "Content-Type": logo_response.headers.get("content-type", "image/jpeg")
            }
            media_response = requests.post(WP_MEDIA_URL, headers=logo_headers, data=logo_response.content, verify=False)
            media_response.raise_for_status()
            attachment_id = media_response.json().get("id", 0)
            logger.info(f"Uploaded logo for {company_name}, Attachment ID: {attachment_id}")
        except Exception as e:
            logger.error(f"Failed to upload logo for {company_name}: {str(e)}")

    post_data = {
        "title": company_name,
        "content": company_details,
        "status": "publish",
        "featured_media": attachment_id,
        "meta": {
            "_company_name": sanitize_text(company_name),
            "_company_logo": str(attachment_id) if attachment_id else "",
            "_company_website": sanitize_text(company_website, is_url=True),
            "_company_industry": sanitize_text(company_industry),
            "_company_founded": sanitize_text(company_founded),
            "_company_type": sanitize_text(company_type),
            "_company_address": sanitize_text(company_address),
            "_company_tagline": sanitize_text(company_details),
            "_company_twitter": "",
            "_company_video": ""
        }
    }
    response = None
    try:
        response = requests.post(WP_COMPANY_URL, json=post_data, headers=wp_headers, timeout=15, verify=False)
        response.raise_for_status()
        post = response.json()
        logger.info(f"Successfully posted company {company_name}: Post ID {post.get('id')}, URL {post.get('link')}")
        return post.get("id"), post.get("link")
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to post company {company_name}: {str(e)}, Status: {response.status_code if response else 'None'}, Response: {response.text if response else 'None'}")
        return None, None

def save_article_to_wordpress(index, job_data, company_id, auth_headers):
    job_title = job_data.get("job_title", "")
    job_description = job_data.get("job_description", "")
    job_type = job_data.get("job_type", "")
    location = job_data.get("location", "Mauritius")
    job_url = job_data.get("job_url", "")
    company_name = job_data.get("company_name", "")
    company_logo = job_data.get("company_logo", "")
    environment = job_data.get("environment", "").lower()
    job_salary = job_data.get("job_salary", "")
    company_industry = job_data.get("company_industry", "")
    company_founded = job_data.get("company_founded", "")
    
    # Check if job already exists on WordPress
    existing_post_id, existing_post_url = check_existing_job(job_title, company_name, auth_headers)
    if existing_post_id:
        logger.info(f"Skipping duplicate job: {job_title} at {company_name}, already posted with Post ID: {existing_post_id}")
        print(f"Job '{job_title}' at {company_name} skipped - already posted on WordPress. Post ID: {existing_post_id}, URL: {existing_post_url}")
        return existing_post_id, existing_post_url

    application = ''
    if '@' in job_data.get("description_application_info", ""):
        application = job_data.get("description_application_info", "")
    elif job_data.get("resolved_application_url", ""):
        application = job_data.get("resolved_application_url", "")
    else:
        application = job_data.get("application_url", "")
        if not application:
            logger.warning(f"No valid application email or URL found for job {job_title}")

    attachment_id = 0
    if company_logo:
        try:
            logo_response = requests.get(company_logo, headers=headers, timeout=10)
            logo_response.raise_for_status()
            logo_headers = {
                "Authorization": auth_headers["Authorization"],
                "Content-Disposition": f'attachment; filename="{company_name}_logo_job_{index}.jpg"',
                "Content-Type": logo_response.headers.get("content-type", "image/jpeg")
            }
            media_response = requests.post(WP_MEDIA_URL, headers=logo_headers, data=logo_response.content, verify=False)
            media_response.raise_for_status()
            attachment_id = media_response.json().get("id", 0)
            logger.info(f"Uploaded logo for job {job_title}, Attachment ID: {attachment_id}")
        except Exception as e:
            logger.error(f"Failed to upload logo for job {job_title}: {str(e)}")

    post_data = {
        "title": sanitize_text(job_title),
        "content": job_description,
        "status": "publish",
        "featured_media": attachment_id,
        "meta": {
            "_job_title": sanitize_text(job_title),
            "_job_location": sanitize_text(location),
            "_job_type": sanitize_text(job_type),
            "_job_description": job_description,
            "_job_salary": sanitize_text(job_salary),
            "_application": sanitize_text(application, is_url=('@' not in application)),
            "_company_id": str(company_id) if company_id else "",
            "_company_name": sanitize_text(company_name),
            "_company_website": sanitize_text(job_data.get("company_website_url", ""), is_url=True),
            "_company_logo": str(attachment_id) if attachment_id else "",
            "_company_tagline": sanitize_text(job_data.get("company_details", "")),
            "_company_address": sanitize_text(job_data.get("company_address", "")),
            "_company_industry": sanitize_text(company_industry),
            "_company_founded": sanitize_text(company_founded),
            "_company_twitter": "",
            "_company_video": ""
        },
        "job_listing_type": [job_type_id] if (job_type_id := get_or_create_term(job_type, "job_type", WP_JOB_TYPE_URL, auth_headers)) else [],
        "job_listing_region": [job_region_id] if (job_region_id := get_or_create_term(location, "job_region", WP_JOB_REGION_URL, auth_headers)) else []
    }
    
    logger.info(f"Final job post payload for {job_title}: {json.dumps(post_data, indent=2)[:200]}...")
    
    try:
        response = requests.post(WP_URL, json=post_data, headers=auth_headers, timeout=15, verify=False)
        response.raise_for_status()
        post = response.json()
        logger.info(f"Successfully posted job {job_title}: Post ID {post.get('id')}, URL {post.get('link')}")
        return post.get("id"), post.get("link")
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to post job {job_title}: {str(e)}, Status: {response.status_code if response else 'None'}, Response: {response.text if response else 'None'}")
        return None, None

def load_processed_ids():
    """Load processed job IDs from file."""
    processed_ids = set()
    try:
        if os.path.exists(PROCESSED_IDS_FILE):
            with open(PROCESSED_IDS_FILE, "r") as f:
                processed_ids = set(line.strip() for line in f if line.strip())
            logger.info(f"Loaded {len(processed_ids)} processed job IDs from {PROCESSED_IDS_FILE}")
    except Exception as e:
        logger.error(f"Failed to load processed IDs from {PROCESSED_IDS_FILE}: {str(e)}")
    return processed_ids

def save_processed_id(job_id):
    """Append a single job ID to the processed IDs file."""
    try:
        with open(PROCESSED_IDS_FILE, "a") as f:
            f.write(f"{job_id}\n")
        logger.info(f"Saved job ID {job_id} to {PROCESSED_IDS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save job ID {job_id} to {PROCESSED_IDS_FILE}: {str(e)}")

def load_last_page():
    """Load the last processed page number."""
    try:
        if os.path.exists(LAST_PAGE_FILE):
            with open(LAST_PAGE_FILE, "r") as f:
                page = int(f.read().strip())
                logger.info(f"Loaded last processed page: {page}")
                return page
    except Exception as e:
        logger.error(f"Failed to load last page from {LAST_PAGE_FILE}: {str(e)}")
    return 0

def save_last_page(page):
    """Save the last processed page number."""
    try:
        with open(LAST_PAGE_FILE, "w") as f:
            f.write(str(page))
        logger.info(f"Saved last processed page: {page} to {LAST_PAGE_FILE}")
    except Exception as e:
        logger.error(f"Failed to save last page to {LAST_PAGE_FILE}: {str(e)}")

def save_to_json(job_dict, company_id, job_post_id, job_post_url, company_url):
    """Save job and company data to scraped_jobs.json."""
    try:
        # Ensure the JSON file exists and is initialized as a list
        json_file = "scraped_jobs.json"
        json_data = []
        if os.path.exists(json_file):
            try:
                with open(json_file, "r", encoding='utf-8') as f:
                    json_data = json.load(f)
                    if not isinstance(json_data, list):
                        json_data = [json_data]
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Error reading {json_file}: {str(e)}. Initializing as empty list.")
                json_data = []

        # Create the data entry
        data = {
            "job_id": generate_job_id(job_dict.get("job_title", ""), job_dict.get("company_name", "")),
            "job_data": job_dict,
            "company_id": company_id,
            "job_post_id": job_post_id,
            "job_post_url": job_post_url,
            "company_url": company_url,
            "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # Append new data
        json_data.append(data)
        
        # Write back to file with proper permissions
        with open(json_file, "w", encoding='utf-8') as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
            f.flush()  # Ensure data is written to disk
        logger.info(f"Successfully saved job data to {json_file}: {job_dict.get('job_title', '')} at {job_dict.get('company_name', '')}")
    except Exception as e:
        logger.error(f"Failed to save to {json_file}: {str(e)}", exc_info=True)
        raise  # Re-raise to catch issues in testing

def crawl(auth_headers, processed_ids):
    success_count = 0
    failure_count = 0
    total_jobs = 0
    start_page = load_last_page()
    
    for i in range(start_page, 15):  # Adjust range to continue from last page
        url = f'https://www.linkedin.com/jobs/search?keywords=&location=Mauritius&start={i * 25}'
        logger.info(f'Fetching job search page: {url}')
        time.sleep(random.uniform(5, 10))
        try:
            session = requests.Session()
            retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
            session.mount('https://', HTTPAdapter(max_retries=retries))
            response = session.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            if "login" in response.url or "challenge" in response.url:
                logger.error("Login or CAPTCHA detected, stopping crawl")
                break
            soup = BeautifulSoup(response.text, 'html.parser')
            job_list = soup.select("#main-content > section > ul > li > div > a")
            urls = [a['href'] for a in job_list if a.get('href')]
            logger.info(f'Found {len(urls)} job URLs on page: {url}')
            
            for index, job_url in enumerate(urls):
                job_data = scrape_job_details(job_url)
                if not job_data:
                    logger.error(f"No data scraped for job: {job_url}")
                    print(f"Job (URL: {job_url}) failed to scrape: No data returned")
                    failure_count += 1
                    total_jobs += 1
                    continue
                
                job_dict = {
                    "job_title": job_data[0],
                    "company_logo": job_data[1],
                    "company_name": job_data[2],
                    "company_url": job_data[3],
                    "location": job_data[4],
                    "environment": job_data[5],
                    "job_type": job_data[6],
                    "level": job_data[7],
                    "job_functions": job_data[8],
                    "industries": job_data[9],
                    "job_description": job_data[10],
                    "job_url": job_data[11],
                    "company_details": job_data[12],
                    "company_website_url": job_data[13],
                    "company_industry": job_data[14],
                    "company_size": job_data[15],
                    "company_headquarters": job_data[16],
                    "company_type": job_data[17],
                    "company_founded": job_data[18],
                    "company_specialties": job_data[19],
                    "company_address": job_data[20],
                    "application_url": job_data[21],
                    "description_application_info": job_data[22],
                    "resolved_application_info": job_data[23],
                    "final_application_email": job_data[24],
                    "final_application_url": job_data[25],
                    "resolved_application_url": job_data[26],
                    "job_salary": ""  # Not scraped, placeholder
                }
                
                job_title = job_dict.get("job_title", "Unknown Job")
                company_name = job_dict.get("company_name", "")
                
                job_id = generate_job_id(job_title, company_name)
                
                if job_id in processed_ids:
                    logger.info(f"Skipping already processed job: {job_id} ({job_title} at {company_name})")
                    print(f"Job '{job_title}' at {company_name} (ID: {job_id}) skipped - already processed.")
                    total_jobs += 1
                    continue
                
                if not company_name or company_name.lower() == "unknown":
                    logger.info(f"Skipping job with
