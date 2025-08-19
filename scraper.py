import requests
from bs4 import BeautifulSoup
import logging
import time
import re
from urllib.parse import urlparse, parse_qs, unquote
import base64
import json
import hashlib
import random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import os
import argparse

# Configure command-line arguments
parser = argparse.ArgumentParser(description="LinkedIn job scraper for WordPress")
parser.add_argument("--wp-base-url", required=True, help="WordPress base URL (e.g., https://your-site.com)")
parser.add_argument("--wp-username", required=True, help="WordPress username for API authentication")
parser.add_argument("--wp-app-password", required=True, help="WordPress application password for API authentication")
parser.add_argument("--scrape-location", required=True, help="Location for job scraping (e.g., Worldwide)")
parser.add_argument("--wp-rest-nonce", required=True, help="WordPress REST API nonce")
args = parser.parse_args()

# Get GitHub token from environment (optional)
github_token = os.getenv("GITHUB_TOKEN")

# Assign arguments to variables
base_url = args.wp_base_url.rstrip('/')
wp_username = args.wp_username
wp_app_password = args.wp_app_password
scrape_location = args.scrape_location
wp_rest_nonce = args.wp_rest_nonce

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s,%(msecs)d - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize results list for JSON output
scrape_results = []

# HTTP headers for scraping
headers = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36'
}

# Constants for WordPress
WP_URL = f"{base_url}/wp-json/wp/v2/staging-scraped"
WP_MEDIA_URL = f"{base_url}/wp-json/wp/v2/media"
WP_JOB_TYPE_URL = f"{base_url}/wp-json/wp/v2/job_listing_type"
WP_JOB_REGION_URL = f"{base_url}/wp-json/wp/v2/job_listing_region"
PROCESSED_IDS_FILE = "processed_job_ids.csv"
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
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', '', text)
    return text.lower()

def generate_job_id(job_title, company_name):
    combined = f"{job_title}_{company_name}"
    return hashlib.md5(combined.encode()).hexdigest()[:16]

def split_paragraphs(text, max_length=200):
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
        response = requests.get(check_url, headers=auth_headers, timeout=10, verify=True)
        response.raise_for_status()
        terms = response.json()
        for term in terms:
            if term['name'].lower() == term_name.lower():
                return term['id']
        post_data = {"name": term_name, "slug": term_name.lower().replace(' ', '-')}
        response = requests.post(wp_url, json=post_data, headers=auth_headers, timeout=10, verify=True)
        response.raise_for_status()
        term = response.json()
        logger.info(f"Created new {taxonomy} term: {term_name}, ID: {term['id']}")
        return term['id']
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to get or create {taxonomy} term {term_name}: {str(e)}")
        return None

def check_existing_entry(title, scraped_type, company_name, auth_headers):
    check_url = f"{WP_URL}?search={title}&_fields=id,link,meta&context=view"
    try:
        response = requests.get(check_url, headers=auth_headers, timeout=10, verify=True)
        response.raise_for_status()
        posts = response.json()
        for post in posts:
            meta = post.get('meta', {})
            if meta.get('_scraped_type') == scraped_type and meta.get('_company_name') == company_name:
                if scraped_type == 'job' and meta.get('_job_title') == title:
                    logger.info(f"Found existing {scraped_type} {title}: Post ID {post.get('id')}")
                    return post.get('id'), post.get('link')
                elif scraped_type == 'company':
                    logger.info(f"Found existing {scraped_type} {title}: Post ID {post.get('id')}")
                    return post.get('id'), post.get('link')
        return None, None
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to check existing {scraped_type} {title}: {str(e)}")
        return None, None

def save_company_to_wordpress(index, company_data, auth_headers):
    company_name = company_data.get("company_name", "")
    company_details = company_data.get("company_details", "")
    company_logo = company_data.get("company_logo", "")
    company_website = company_data.get("company_website_url", "")
    company_industry = company_data.get("company_industry", "")
    company_founded = company_data.get("company_founded", "")
    company_type = company_data.get("company_type", "")
    company_address = company_data.get("company_address", "")
    
    result = {
        "type": "company",
        "job_id": "",
        "job_title": "",
        "company_name": company_name,
        "post_id": "",
        "status": "",
        "error": ""
    }

    existing_id, existing_url = check_existing_entry(company_name, 'company', company_name, auth_headers)
    if existing_id:
        logger.info(f"Skipping duplicate company: {company_name}, Post ID: {existing_id}")
        result.update({
            "post_id": str(existing_id),
            "status": "skipped",
            "error": "Duplicate company"
        })
        scrape_results.append(result)
        print(f"Company '{company_name}' skipped - already posted. Post ID: {existing_id}")
        return existing_id, existing_url

    attachment_id = 0
    if company_logo:
        try:
            logo_response = requests.get(company_logo, headers=headers, timeout=10, verify=True)
            logo_response.raise_for_status()
            logo_headers = {
                "Authorization": auth_headers["Authorization"],
                "Content-Disposition": f'attachment; filename="{company_name}_logo.jpg"',
                "Content-Type": logo_response.headers.get("content-type", "image/jpeg"),
                "X-WP-Nonce": auth_headers["X-WP-Nonce"]
            }
            media_response = requests.post(WP_MEDIA_URL, headers=logo_headers, data=logo_response.content, timeout=10, verify=True)
            media_response.raise_for_status()
            attachment_id = media_response.json().get("id", 0)
            logger.info(f"Uploaded logo for {company_name}, Attachment ID: {attachment_id}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to upload logo for {company_name}: {str(e)}")
            result["error"] = f"Logo upload failed: {str(e)}"
            scrape_results.append(result)

    post_data = {
        "title": company_name,
        "content": company_details,
        "status": "publish",
        "featured_media": attachment_id,
        "meta": {
            "_scraped_type": "company",
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
    logger.debug(f"Company post data for {company_name}: {json.dumps(post_data, indent=2)}")
    try:
        response = requests.post(WP_URL, json=post_data, headers=auth_headers, timeout=15, verify=True)
        response.raise_for_status()
        post = response.json()
        result.update({
            "post_id": str(post.get("id")),
            "status": "success"
        })
        logger.info(f"Successfully posted company {company_name}: Post ID {post.get('id')}")
        scrape_results.append(result)
        return post.get("id"), post.get("link")
    except requests.exceptions.HTTPError as e:
        error_message = f"Failed to post company {company_name}: {str(e)}\nResponse: {response.text}"
        logger.error(error_message)
        result.update({
            "status": "failed",
            "error": error_message
        })
        scrape_results.append(result)
        return None, None
    except requests.exceptions.RequestException as e:
        error_message = f"Failed to post company {company_name}: {str(e)}"
        logger.error(error_message)
        result.update({
            "status": "failed",
            "error": error_message
        })
        scrape_results.append(result)
        return None, None

def save_job_to_wordpress(index, job_data, company_id, auth_headers):
    job_title = job_data.get("job_title", "")
    job_description = job_data.get("job_description", "")
    job_type = job_data.get("job_type", "")
    location = job_data.get("location", scrape_location)
    company_name = job_data.get("company_name", "")
    company_logo = job_data.get("company_logo", "")
    environment = job_data.get("environment", "").lower()
    job_salary = job_data.get("job_salary", "")
    company_industry = job_data.get("company_industry", "")
    company_founded = job_data.get("company_founded", "")
    
    job_id = generate_job_id(job_title, company_name)
    result = {
        "type": "job",
        "job_id": job_id,
        "job_title": job_title,
        "company_name": company_name,
        "post_id": "",
        "status": "",
        "error": ""
    }

    existing_id, existing_url = check_existing_entry(job_title, 'job', company_name, auth_headers)
    if existing_id:
        logger.info(f"Skipping duplicate job: {job_title} at {company_name}, Post ID: {existing_id}")
        result.update({
            "post_id": str(existing_id),
            "status": "skipped",
            "error": "Duplicate job"
        })
        scrape_results.append(result)
        print(f"Job '{job_title}' at {company_name} skipped - already posted. Post ID: {existing_id}")
        return existing_id, existing_url

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
            logo_response = requests.get(company_logo, headers=headers, timeout=10, verify=True)
            logo_response.raise_for_status()
            logo_headers = {
                "Authorization": auth_headers["Authorization"],
                "Content-Disposition": f'attachment; filename="{company_name}_logo_job_{index}.jpg"',
                "Content-Type": logo_response.headers.get("content-type", "image/jpeg"),
                "X-WP-Nonce": auth_headers["X-WP-Nonce"]
            }
            media_response = requests.post(WP_MEDIA_URL, headers=logo_headers, data=logo_response.content, timeout=10, verify=True)
            media_response.raise_for_status()
            attachment_id = media_response.json().get("id", 0)
            logger.info(f"Uploaded logo for job {job_title}, Attachment ID: {attachment_id}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to upload logo for job {job_title}: {str(e)}")
            result["error"] = f"Logo upload failed: {str(e)}"
            scrape_results.append(result)

    post_data = {
        "title": sanitize_text(job_title),
        "content": job_description,
        "status": "publish",
        "featured_media": attachment_id,
        "meta": {
            "_scraped_type": "job",
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
    logger.debug(f"Job post data for {job_title}: {json.dumps(post_data, indent=2)}")
    try:
        response = requests.post(WP_URL, json=post_data, headers=auth_headers, timeout=15, verify=True)
        response.raise_for_status()
        post = response.json()
        result.update({
            "post_id": str(post.get("id")),
            "status": "success"
        })
        logger.info(f"Successfully posted job {job_title}: Post ID {post.get('id')}")
        scrape_results.append(result)
        return post.get("id"), post.get("link")
    except requests.exceptions.HTTPError as e:
        error_message = f"Failed to post job {job_title}: {str(e)}\nResponse: {response.text}"
        logger.error(error_message)
        result.update({
            "status": "failed",
            "error": error_message
        })
        scrape_results.append(result)
        return None, None
    except requests.exceptions.RequestException as e:
        error_message = f"Failed to post job {job_title}: {str(e)}"
        logger.error(error_message)
        result.update({
            "status": "failed",
            "error": error_message
        })
        scrape_results.append(result)
        return None, None

def save_results_to_json():
    try:
        with open("scrape_results.json", "w") as f:
            json.dump(scrape_results, f, indent=2)
        logger.info("Saved scrape results to scrape_results.json")
    except Exception as e:
        logger.error(f"Failed to save scrape results: {str(e)}")

def load_processed_ids():
    processed_ids = set()
    try:
        if os.path.exists(PROCESSED_IDS_FILE):
            with open(PROCESSED_IDS_FILE, "r") as f:
                processed_ids = set(line.strip() for line in f if line.strip())
            logger.info(f"Loaded {len(processed_ids)} processed job IDs")
        return processed_ids
    except Exception as e:
        logger.error(f"Failed to load processed IDs: {str(e)}")
        return set()

def save_processed_id(job_id):
    try:
        with open(PROCESSED_IDS_FILE, "a") as f:
            f.write(f"{job_id}\n")
        logger.info(f"Saved job ID {job_id}")
    except Exception as e:
        logger.error(f"Failed to save job ID {job_id}: {str(e)}")

def load_last_page():
    try:
        if os.path.exists(LAST_PAGE_FILE):
            with open(LAST_PAGE_FILE, "r") as f:
                page = int(f.read().strip())
                logger.info(f"Loaded last processed page: {page}")
                return page
        return 0
    except Exception as e:
        logger.error(f"Failed to load last page: {str(e)}")
        return 0

def save_last_page(page):
    try:
        with open(LAST_PAGE_FILE, "w") as f:
            f.write(str(page))
        logger.info(f"Saved last processed page: {page}")
    except Exception as e:
        logger.error(f"Failed to save last page: {str(e)}")

def scrape_job_details(job_url):
    logger.info(f'Fetching job details from: {job_url}')
    try:
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        session.mount('https://', HTTPAdapter(max_retries=retries))
        response = session.get(job_url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        job_title = soup.select_one("h1.top-card-layout__title")
        job_title = job_title.get_text().strip() if job_title else ''
        logger.info(f'Scraped Job Title: {job_title}')

        company_logo = soup.select_one("#main-content > section.core-rail.mx-auto.papabear\:w-core-rail-width.mamabear\:max-w-\[790px\].babybear\:max-w-\[790px\] > div > section.top-card-layout.container-lined.overflow-hidden.babybear\:rounded-\[0px\] > div > a > img")
        company_logo = (company_logo.get('data-delayed-url') or company_logo.get('src') or '') if company_logo else ''
        logger.info(f'Scraped Company Logo URL: {company_logo}')

        company_name = soup.select_one(".topcard__org-name-link")
        company_name = company_name.get_text().strip() if company_name else ''
        logger.info(f'Scraped Company Name: {company_name}')

        company_url = soup.select_one(".topcard__org-name-link")
        company_url = company_url['href'] if company_url and company_url.get('href') else ''
        if company_url:
            company_url = re.sub(r'\?.*$', '', company_url)
            logger.info(f'Scraped Company URL: {company_url}')

        location = soup.select_one(".topcard__flavor.topcard__flavor--bullet")
        location = location.get_text().strip() if location else scrape_location
        location_parts = [part.strip() for part in location.split(',') if part.strip()]
        location = ', '.join(dict.fromkeys(location_parts))
        logger.info(f'Deduplicated location for {job_title}: {location}')

        environment = ''
        env_elements = soup.select(".topcard__flavor--metadata")
        for elem in env_elements:
            text = elem.get_text().strip().lower()
            if 'remote' in text or 'hybrid' in text or 'on-site' in text:
                environment = elem.get_text().strip()
                break
        logger.info(f'Scraped Environment: {environment}')

        level = soup.select_one(".description__job-criteria-list > li:nth-child(1) > span")
        level = level.get_text().strip() if level else ''
        logger.info(f'Scraped Level: {level}')

        job_type = soup.select_one(".description__job-criteria-list > li:nth-child(2) > span")
        job_type = job_type.get_text().strip() if job_type else ''
        job_type = FRENCH_TO_ENGLISH_JOB_TYPE.get(job_type, job_type)
        logger.info(f'Scraped Type: {job_type}')

        job_functions = soup.select_one(".description__job-criteria-list > li:nth-child(3) > span")
        job_functions = job_functions.get_text().strip() if job_functions else ''
        logger.info(f'Scraped Job Functions: {job_functions}')

        industries = soup.select_one(".description__job-criteria-list > li:nth-child(4) > span")
        industries = industries.get_text().strip() if industries else ''
        logger.info(f'Scraped Industries: {industries}')

        job_description = ''
        description_container = soup.select_one(".show-more-less-html__markup")
        if description_container:
            paragraphs = description_container.find_all(['p', 'li'], recursive=False)
            if paragraphs:
                seen = set()
                unique_paragraphs = []
                for p in paragraphs:
                    para = sanitize_text(p.get_text().strip())
                    if not para:
                        continue
                    norm_para = normalize_for_deduplication(para)
                    if norm_para and norm_para not in seen:
                        unique_paragraphs.append(para)
                        seen.add(norm_para)
                job_description = '\n\n'.join(unique_paragraphs)
            else:
                raw_text = description_container.get_text(separator='\n').strip()
                paragraphs = [para.strip() for para in raw_text.split('\n\n') if para.strip()]
                seen = set()
                unique_paragraphs = []
                for para in paragraphs:
                    para = sanitize_text(para)
                    if not para:
                        continue
                    norm_para = normalize_for_deduplication(para)
                    if norm_para and norm_para not in seen:
                        unique_paragraphs.append(para)
                        seen.add(norm_para)
                job_description = '\n\n'.join(unique_paragraphs)
            job_description = re.sub(r'(?i)(?:\s*Show\s+more\s*$|\s*Show\s+less\s*$)', '', job_description, flags=re.MULTILINE).strip()
            job_description = split_paragraphs(job_description, max_length=200)
            logger.info(f'Scraped Job Description (length): {len(job_description)}')

        description_application_info = ''
        description_application_url = ''
        if description_container:
            email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
            emails = re.findall(email_pattern, job_description)
            if emails:
                description_application_info = emails[0]
                logger.info(f'Found email in job description: {description_application_info}')
            else:
                links = description_container.find_all('a', href=True)
                for link in links:
                    href = link['href']
                    if 'apply' in href.lower() or 'careers' in href.lower() or 'jobs' in href.lower():
                        description_application_url = href
                        description_application_info = href
                        logger.info(f'Found application link in job description: {description_application_info}')
                        break

        application_anchor = soup.select_one("#teriary-cta-container > div > a")
        application_url = application_anchor['href'] if application_anchor and application_anchor.get('href') else None
        logger.info(f'Scraped Application URL: {application_url}')

        resolved_application_info = ''
        resolved_application_url = ''
        final_application_email = description_application_info if description_application_info and '@' in description_application_info else ''
        final_application_url = description_application_url if description_application_url else ''

        if application_url:
            try:
                time.sleep(5)
                resp_app = session.get(application_url, headers=headers, timeout=15, allow_redirects=True, verify=True)
                resolved_application_url = resp_app.url
                logger.info(f'Resolved Application URL: {resolved_application_url}')
                
                app_soup = BeautifulSoup(resp_app.text, 'html.parser')
                email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
                emails = re.findall(email_pattern, resp_app.text)
                if emails:
                    resolved_application_info = emails[0]
                else:
                    links = app_soup.find_all('a', href=True)
                    for link in links:
                        href = link['href']
                        if 'apply' in href.lower() or 'careers' in href.lower() or 'jobs' in href.lower():
                            resolved_application_info = href
                            break

                if final_application_email and resolved_application_info and '@' in resolved_application_info:
                    final_application_email = final_application_email
                elif resolved_application_info and '@' in resolved_application_info:
                    final_application_email = resolved_application_info

                if description_application_url and resolved_application_url:
                    final_application_url = description_application_url if description_application_url == resolved_application_url else resolved_application_url
                elif resolved_application_url:
                    final_application_url = resolved_application_url

            except Exception as e:
                logger.error(f'Failed to follow application URL: {str(e)}')
                final_application_url = description_application_url if description_application_url else application_url or ''

        company_details = ''
        company_website_url = ''
        company_industry = ''
        company_size = ''
        company_headquarters = ''
        company_type = ''
        company_founded = ''
        company_specialties = ''
        company_address = ''

        if company_url:
            logger.info(f'Fetching company page: {company_url}')
            try:
                company_response = session.get(company_url, headers=headers, timeout=15)
                company_response.raise_for_status()
                company_soup = BeautifulSoup(company_response.text, 'html.parser')

                company_details_elem = company_soup.select_one("p.about-us__description") or company_soup.select_one("section.core-section-container > div > p")
                company_details = company_details_elem.get_text().strip() if company_details_elem else ''

                company_website_anchor = company_soup.select_one("dl > div:nth-child(1) > dd > a")
                company_website_url = company_website_anchor['href'] if company_website_anchor and company_website_anchor.get('href') else ''
                if 'linkedin.com/redir/redirect' in company_website_url:
                    parsed_url = urlparse(company_website_url)
                    query_params = parse_qs(parsed_url.query)
                    if 'url' in query_params:
                        company_website_url = unquote(query_params['url'][0])
                if company_website_url and 'linkedin.com' not in company_website_url:
                    try:
                        time.sleep(5)
                        resp_company_web = session.get(company_website_url, headers=headers, timeout=15, allow_redirects=True, verify=True)
                        company_website_url = resp_company_web.url
                    except Exception:
                        company_website_url = ''
                else:
                    company_website_url = ''

                def get_company_detail(label):
                    elements = company_soup.select("section.core-section-container.core-section-container--with-border > div > dl > div")
                    for elem in elements:
                        dt = elem.find("dt")
                        if dt and dt.get_text().strip().lower() == label.lower():
                            dd = elem.find("dd")
                            return dd.get_text().strip() if dd else ''
                    return ''

                company_industry = get_company_detail("Industry")
                company_size = get_company_detail("Company size")
                company_headquarters = get_company_detail("Headquarters")
                company_type = get_company_detail("Type")
                company_founded = get_company_detail("Founded")
                company_specialties = get_company_detail("Specialties")
                company_address = company_soup.select_one("#address-0")
                company_address = company_address.get_text().strip() if company_address else company_headquarters
            except Exception as e:
                logger.error(f'Error fetching company page: {company_url} - {str(e)}')

        return [
            job_title,
            company_logo,
            company_name,
            company_url,
            location,
            environment,
            job_type,
            level,
            job_functions,
            industries,
            job_description,
            job_url,
            company_details,
            company_website_url,
            company_industry,
            company_size,
            company_headquarters,
            company_type,
            company_founded,
            company_specialties,
            company_address,
            application_url,
            description_application_info,
            resolved_application_info,
            final_application_email,
            final_application_url,
            resolved_application_url
        ]
    except Exception as e:
        logger.error(f"Failed to scrape job details from {job_url}: {str(e)}")
        return None

def crawl(auth_headers, processed_ids):
    success_count = 0
    failure_count = 0
    total_jobs = 0
    start_page = load_last_page()
    
    for i in range(start_page, 15):
        url = f'https://www.linkedin.com/jobs/search?keywords=&location={scrape_location}&start={i * 25}'
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
                scrape_results.append({
                    "type": "system",
                    "job_id": "",
                    "job_title": "",
                    "company_name": "",
                    "post_id": "",
                    "status": "failed",
                    "error": "Login or CAPTCHA detected"
                })
                break
            soup = BeautifulSoup(response.text, 'html.parser')
            job_list = soup.select("#main-content > section > ul > li > div > a")
            urls = [a['href'] for a in job_list if a.get('href')]
            logger.info(f'Found {len(urls)} job URLs on page')
            
            for index, job_url in enumerate(urls):
                job_data = scrape_job_details(job_url)
                if not job_data:
                    logger.error(f"No data scraped for job: {job_url}")
                    scrape_results.append({
                        "type": "job",
                        "job_id": "",
                        "job_title": "Unknown",
                        "company_name": "",
                        "post_id": "",
                        "status": "failed",
                        "error": f"No data scraped for job: {job_url}"
                    })
                    print(f"Job (URL: {job_url}) failed to scrape")
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
                    "job_salary": ""
                }
                
                job_title = job_dict.get("job_title", "Unknown Job")
                company_name = job_dict.get("company_name", "")
                
                job_id = generate_job_id(job_title, company_name)
                
                if job_id in processed_ids:
                    logger.info(f"Skipping processed job: {job_id} ({job_title} at {company_name})")
                    scrape_results.append({
                        "type": "job",
                        "job_id": job_id,
                        "job_title": job_title,
                        "company_name": company_name,
                        "post_id": "",
                        "status": "skipped",
                        "error": "Already processed"
                    })
                    print(f"Job '{job_title}' at {company_name} (ID: {job_id}) skipped - already processed")
                    total_jobs += 1
                    continue
                
                if not company_name or company_name.lower() == "unknown":
                    logger.info(f"Skipping job with unknown company: {job_title} (ID: {job_id})")
                    scrape_results.append({
                        "type": "job",
                        "job_id": job_id,
                        "job_title": job_title,
                        "company_name": "",
                        "post_id": "",
                        "status": "skipped",
                        "error": "Unknown company"
                    })
                    print(f"Job '{job_title}' (ID: {job_id}) skipped - unknown company")
                    failure_count += 1
                    total_jobs += 1
                    continue
                
                total_jobs += 1
                
                company_id, company_url = save_company_to_wordpress(index, job_dict, auth_headers)
                job_post_id, job_post_url = save_job_to_wordpress(index, job_dict, company_id, auth_headers)
                
                if job_post_id:
                    processed_ids.add(job_id)
                    save_processed_id(job_id)
                    logger.info(f"Processed job: {job_id} - {job_title} at {company_name}")
                    print(f"Job '{job_title}' at {company_name} (ID: {job_id}) posted. Post ID: {job_post_id}")
                    success_count += 1
                else:
                    failure_count += 1
            
            save_last_page(i)
        
        except Exception as e:
            logger.error(f'Error fetching job search page: {url} - {str(e)}')
            scrape_results.append({
                "type": "system",
                "job_id": "",
                "job_title": "",
                "company_name": "",
                "post_id": "",
                "status": "failed",
                "error": f"Error fetching job search page: {url} - {str(e)}"
            })
            failure_count += 1
    
    print("\n--- Summary ---")
    print(f"Total jobs processed: {total_jobs}")
    print(f"Successfully posted: {success_count}")
    print(f"Failed to post or scrape: {failure_count}")
    save_results_to_json()

def main():
    auth_string = f"{wp_username}:{wp_app_password}"
    auth = base64.b64encode(auth_string.encode()).decode()
    wp_headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-WP-Nonce": wp_rest_nonce
    }
    
    # Test authentication only (skip permission test to allow crawling)
    try:
        test_response = requests.get(f"{base_url}/wp-json/wp/v2/users/me", headers=wp_headers, timeout=10, verify=True)
        test_response.raise_for_status()
        user_data = test_response.json()
        logger.info(f"Authentication successful for user: {wp_username} (ID: {user_data.get('id')})")
    except requests.exceptions.HTTPError as e:
        logger.error(f"Authentication failed: {str(e)}\nResponse: {test_response.text}")
        scrape_results.append({
            "type": "system",
            "job_id": "",
            "job_title": "",
            "company_name": "",
            "post_id": "",
            "status": "failed",
            "error": f"Authentication failed: {test_response.text}"
        })
        save_results_to_json()
        return
    except requests.exceptions.RequestException as e:
        logger.error(f"Authentication test failed: {str(e)}")
        scrape_results.append({
            "type": "system",
            "job_id": "",
            "job_title": "",
            "company_name": "",
            "post_id": "",
            "status": "failed",
            "error": f"Authentication test failed: {str(e)}"
        })
        save_results_to_json()
        return
    
    # Proceed with crawling even if permission test would fail
    processed_ids = load_processed_ids()
    crawl(auth_headers=wp_headers, processed_ids=processed_ids)

if __name__ == "__main__":
    main()
```

**Changes Made**:
- **Version Update**: New `artifact_version_id="d8e3c4f2-5b79-4e8c-9b1d-6e7f3a5b4c0d"`.
- **Bypass Permission Test**: Removed the `POST` test to `WP_URL` in `main()` to allow the scraper to proceed with crawling and posting jobs/companies even if the test post fails.
- **Enhanced Error Logging**: Added full response text to error messages in `save_company_to_wordpress` and `save_job_to_wordpress` for better debugging (e.g., `Failed to post job ...: Response: {...}`).
- **Kept Nonce**: Retained the `X-WP-Nonce` header but can be commented out for testing if needed:
  ```python
  # "X-WP-Nonce": wp_rest_nonce
  ```
- **Results Structure**: Ensured `scrape_results.json` includes detailed error messages for failed posts, e.g.:
  ```json
  {
    "type": "job",
    "job_id": "a1b2c3d4e5f67890",
    "job_title": "Software Engineer",
    "company_name": "Tech Corp",
    "post_id": "",
    "status": "failed",
    "error": "Failed to post job Software Engineer: 403 Client Error: Forbidden for url: https://mauritius.mimusjobs.com/wp-json/wp/v2/staging-scraped\nResponse: {\"code\":\"rest_cannot_create\",\"message\":\"Sorry, you are not allowed to create posts as this user.\",\"data\":{\"status\":403}}"
  }
  ```

**Action**:
- Update `scraper.py` in the `mirriam/project-li` repository.
- Ensure the GitHub Actions workflow (`scraper.yml`, `artifact_id="4395fb32-7dc8-425e-83d7-7d5e42ebde9b"`) passes the correct environment variables:
  ```yaml
  env:
    WP_BASE_URL: ${{ secrets.WP_BASE_URL }}
    WP_USERNAME: ${{ secrets.WP_USERNAME }}
    WP_APP_PASSWORD: ${{ secrets.WP_APP_PASSWORD }}
    SCRAPE_LOCATION: ${{ inputs.scrape_location }}
    WP_REST_NONCE: ${{ inputs.wp_rest_nonce }}
  ```
- Commit and push the updated `scraper.py` to trigger a new workflow run.

#### Step 3: Update Plugin to Display `scrape_results.json` Directly
The `Scraped_Data_Staging` plugin (version 1.6.8, `artifact_id="f84cd93e-6ba4-4037-932b-593c868ff5a6"`, `artifact_version_id="49572bc3-975b-4a32-b41b-3ebf084e09ee"`) already fetches `scrape_results.json` and displays a table. However, to ensure the table shows all scraped data (including failed posts), we’ll update the `settings_page_callback` method to handle cases where posts weren’t created. We’ll also add a status message to clarify authentication issues.

Here’s the updated plugin file (only showing the changed parts for brevity):

<xaiArtifact artifact_id="f84cd93e-6ba4-4037-932b-593c868ff5a6" artifact_version_id="21768b92-7cdf-42bb-b2ee-5db012ba9528" title="scraped-data-staging.php" contentType="application/x-php">
```php
<?php
/**
 * Plugin Name: Scraped Data Staging
 * Plugin URI: https://example.com/scraped-data-staging
 * Description: A WordPress plugin to stage scraped company and job data from a GitHub-hosted scraper script in a single post type for review before publishing. Settings are managed under the plugin menu, manual post creation is disabled, and GitHub authentication uses only a personal access token with a hidden repository. Displays scraper results in a table after running.
 * Version: 1.6.9
 * Author: Grok
 * Author URI: https://x.ai
 * License: GPL-2.0+
 * Text Domain: scraped-data-staging
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit; // Prevent direct access.
}

/**
 * Class to manage the Scraped Data Staging plugin.
 */
class Scraped_Data_Staging {
    // ... (previous methods unchanged: init, register_post_type, register_meta, add_approve_meta_box, handle_approval, add_admin_columns, populate_admin_columns, remove_row_actions, hide_add_new_button, add_settings_page, register_settings, field_callback, check_github_connection, trigger_scraper, get_workflow_results, refresh_results)

    /**
     * Render settings page with GitHub connection status, trigger button, and results table.
     */
    public static function settings_page_callback() {
        $connection = self::check_github_connection();
        $success = isset( $_GET['success'] ) ? sanitize_text_field( $_GET['success'] ) : '';
        $error = isset( $_GET['error'] ) ? sanitize_text_field( $_GET['error'] ) : '';
        $results_refreshed = isset( $_GET['results_refreshed'] ) ? true : false;
        $results = $results_refreshed ? self::get_workflow_results() : array( 'status' => 'none', 'message' => '', 'results' => array() );

        // Check WordPress API authentication
        $wp_auth_error = '';
        $wp_username = get_option( 'sds_wp_username', '' );
        $wp_app_password = get_option( 'sds_wp_app_password', '' );
        if ( $wp_username && $wp_app_password ) {
            $auth = base64_encode( $wp_username . ':' . $wp_app_password );
            $response = wp_remote_get( trailingslashit( get_option( 'sds_base_url', get_site_url() ) ) . 'wp-json/wp/v2/users/me', array(
                'headers' => array(
                    'Authorization' => 'Basic ' . $auth,
                    'Content-Type' => 'application/json',
                    'Accept' => 'application/json',
                ),
                'timeout' => 10,
            ) );
            if ( is_wp_error( $response ) || wp_remote_retrieve_response_code( $response ) !== 200 ) {
                $wp_auth_error = __( 'WordPress API authentication failed. Check username and application password.', 'scraped-data-staging' );
                error_log( 'SDS: WP API auth test failed: ' . ( is_wp_error( $response ) ? $response->get_error_message() : wp_remote_retrieve_body( $response ) ) );
            }
        } else {
            $wp_auth_error = __( 'WordPress username or application password is missing.', 'scraped-data-staging' );
        }
        ?>
        <div class="wrap">
            <h1><?php esc_html_e( 'Scraper Settings', 'scraped-data-staging' ); ?></h1>
            <?php if ( $success ) : ?>
                <div class="notice notice-success is-dismissible"><p><?php echo esc_html( $success ); ?></p></div>
            <?php endif; ?>
            <?php if ( $error ) : ?>
                <div class="notice notice-error is-dismissible"><p><?php echo esc_html( $error ); ?></p></div>
            <?php endif; ?>
            <?php if ( $wp_auth_error ) : ?>
                <div class="notice notice-error is-dismissible"><p><?php echo esc_html( $wp_auth_error ); ?></p></div>
            <?php endif; ?>
            <h2><?php esc_html_e( 'GitHub Connection Status', 'scraped-data-staging' ); ?></h2>
            <p><strong><?php echo $connection['status'] === 'success' ? __( 'Connected', 'scraped-data-staging' ) : __( 'Not Connected', 'scraped-data-staging' ); ?></strong>: <?php echo esc_html( $connection['message'] ); ?></p>
            <form method="post" action="options.php">
                <?php
                settings_fields( 'sds_settings_group' );
                do_settings_sections( 'sds-settings' );
                submit_button();
                ?>
            </form>
            <h2><?php esc_html_e( 'Run Scraper', 'scraped-data-staging' ); ?></h2>
            <form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
                <input type="hidden" name="action" value="sds_trigger_scraper">
                <?php wp_nonce_field( 'sds_trigger_scraper_nonce' ); ?>
                <p>
                    <input type="submit" class="button button-primary" value="<?php esc_attr_e( 'Run Scraper Now', 'scraped-data-staging' ); ?>" <?php echo $connection['status'] !== 'success' || $wp_auth_error ? 'disabled' : ''; ?> />
                    <p class="description"><?php esc_html_e( 'Triggers the scraper script via GitHub Actions.', 'scraped-data-staging' ); ?></p>
                </p>
            </form>
            <h2><?php esc_html_e( 'Scraper Results', 'scraped-data-staging' ); ?></h2>
            <form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
                <input type="hidden" name="action" value="sds_refresh_results">
                <?php wp_nonce_field( 'sds_refresh_results_nonce' ); ?>
                <p>
                    <input type="submit" class="button button-secondary" value="<?php esc_attr_e( 'Refresh Results', 'scraped-data-staging' ); ?>" <?php echo ! get_option( 'sds_last_workflow_run_id', '' ) ? 'disabled' : ''; ?> />
                    <p class="description"><?php esc_html_e( 'Fetches the latest results from the GitHub Actions workflow.', 'scraped-data-staging' ); ?></p>
                </p>
            </form>
            <div class="sds-results">
                <h3><?php esc_html_e( 'Latest Scraper Results', 'scraped-data-staging' ); ?></h3>
                <?php if ( $results['status'] === 'success' && ! empty( $results['results'] ) ) : ?>
                    <table class="wp-list-table widefat fixed striped">
                        <thead>
                            <tr>
                                <th><?php esc_html_e( 'Type', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Job ID', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Job Title', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Company Name', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Post ID', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Status', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Error', 'scraped-data-staging' ); ?></th>
                            </tr>
                        </thead>
                        <tbody>
                            <?php foreach ( $results['results'] as $result ) : ?>
                                <tr>
                                    <td><?php echo esc_html( ucfirst( $result['type'] ?? 'N/A' ) ); ?></td>
                                    <td><?php echo esc_html( $result['job_id'] ?? '' ); ?></td>
                                    <td><?php echo esc_html( $result['job_title'] ?? '' ); ?></td>
                                    <td><?php echo esc_html( $result['company_name'] ?? '' ); ?></td>
                                    <td>
                                        <?php if ( ! empty( $result['post_id'] ) && ( $result['status'] ?? '' ) === 'success' ) : ?>
                                            <a href="<?php echo esc_url( admin_url( 'post.php?post=' . $result['post_id'] . '&action=edit' ) ); ?>">
                                                <?php echo esc_html( $result['post_id'] ); ?>
                                            </a>
                                        <?php else : ?>
                                            <?php echo esc_html( $result['post_id'] ?? '' ); ?>
                                        <?php endif; ?>
                                    </td>
                                    <td><?php echo esc_html( ucfirst( $result['status'] ?? 'N/A' ) ); ?></td>
                                    <td><?php echo esc_html( $result['error'] ?? '' ); ?></td>
                                </tr>
                            <?php endforeach; ?>
                        </tbody>
                    </table>
                <?php elseif ( $results['status'] === 'error' ) : ?>
                    <div class="notice notice-error is-dismissible"><p><?php echo esc_html( $results['message'] ); ?></p></div>
                    <table class="wp-list-table widefat fixed striped">
                        <thead>
                            <tr>
                                <th><?php esc_html_e( 'Type', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Job ID', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Job Title', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Company Name', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Post ID', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Status', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Error', 'scraped-data-staging' ); ?></th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td colspan="7"><?php echo esc_html( $results['message'] ); ?></td>
                            </tr>
                        </tbody>
                    </table>
                <?php else : ?>
                    <table class="wp-list-table widefat fixed striped">
                        <thead>
                            <tr>
                                <th><?php esc_html_e( 'Type', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Job ID', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Job Title', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Company Name', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Post ID', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Status', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Error', 'scraped-data-staging' ); ?></th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td colspan="7"><?php esc_html_e( 'No results available. Run the scraper or click "Refresh Results" to view the latest results.', 'scraped-data-staging' ); ?></td>
                            </tr>
                        </tbody>
                    </table>
                <?php endif; ?>
            </div>
            <style>
                .sds-results table {
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 10px;
                }
                .sds-results th, .sds-results td {
                    padding: 8px;
                    text-align: left;
                    vertical-align: top;
                }
                .sds-results th {
                    background: #f5f5f5;
                    font-weight: bold;
                }
                .sds-results tr:nth-child(even) {
                    background: #f9f9f9;
                }
                .sds-results td {
                    max-width: 300px;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }
                .sds-results td:hover {
                    overflow: visible;
                    white-space: normal;
                    word-break: break-word;
                }
            </style>
        </div>
        <?php
    }
}

// Initialize the plugin.
Scraped_Data_Staging::init();
