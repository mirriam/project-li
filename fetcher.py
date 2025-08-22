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

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

headers = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36'
}

WP_SITE_URL = os.getenv('WP_SITE_URL')
WP_USERNAME = os.getenv('WP_USERNAME')
WP_APP_PASSWORD = os.getenv('WP_APP_PASSWORD')
COUNTRY = os.getenv('COUNTRY')
KEYWORD = os.getenv('KEYWORD', '')
SECRET_TOKEN = os.getenv('SECRET_TOKEN', '')

# WARNING: Scraping LinkedIn may violate their terms of service. Ensure you have permission or use their official API.
# Validate SECRET_TOKEN against your monetization server
if not SECRET_TOKEN:
    logger.error("Missing secret token. Fetcher access denied.")
    print("Missing secret token. Exiting.")
    exit(1)
try:
    response = requests.post('https://your-site.com/validate', json={'token': SECRET_TOKEN}, timeout=5)
    response.raise_for_status()
    if not response.json().get('valid'):
        logger.error("Invalid secret token. Fetcher access denied.")
        print("Invalid secret token. Exiting.")
        exit(1)
except requests.exceptions.RequestException as e:
    logger.error(f"Failed to validate secret token: {str(e)}")
    print("Secret token validation failed. Exiting.")
    exit(1)

WP_SAVE_COMPANY_URL = f"{WP_SITE_URL}/wp-json/fetcher/v1/save-company"
WP_SAVE_JOB_URL = f"{WP_SITE_URL}/wp-json/fetcher/v1/save-job"
WP_FETCHER_STATUS_URL = f"{WP_SITE_URL}/wp-json/fetcher/v1/get-status"
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

def check_fetcher_status(auth_headers):
    try:
        response = requests.get(WP_FETCHER_STATUS_URL, headers=auth_headers, timeout=5)
        response.raise_for_status()
        status = response.json().get('status', 'stopped')
        logger.info(f"Fetcher status check: {status}")
        return status
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to check fetcher status: {str(e)}")
        return 'stopped'

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

def save_company_to_wordpress(index, company_data, wp_headers):
    if check_fetcher_status(wp_headers) != 'running':
        logger.info("Fetcher stopped before saving company")
        return None, None

    company_name = company_data.get("company_name", "")
    company_details = company_data.get("company_details", "")
    company_logo = company_data.get("company_logo", "")
    company_website = company_data.get("company_website_url", "")
    company_industry = company_data.get("company_industry", "")
    company_founded = company_data.get("company_founded", "")
    company_type = company_data.get("company_type", "")
    company_address = company_data.get("company_address", "")
    
    company_id = hashlib.md5(company_name.encode()).hexdigest()[:16]
    
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
            media_response = requests.post(f"{WP_SITE_URL}/wp-json/wp/v2/media", headers=logo_headers, data=logo_response.content)
            media_response.raise_for_status()
            attachment_id = media_response.json().get("id", 0)
            logger.info(f"Uploaded logo for {company_name}, Attachment ID: {attachment_id}")
        except Exception as e:
            logger.error(f"Failed to upload logo for {company_name}: {str(e)}")

    post_data = {
        "company_id": company_id,
        "company_name": sanitize_text(company_name),
        "company_details": sanitize_text(company_details),
        "featured_media": attachment_id,
        "company_website": sanitize_text(company_website, is_url=True),
        "company_industry": sanitize_text(company_industry),
        "company_founded": sanitize_text(company_founded),
        "company_type": sanitize_text(company_type),
        "company_address": sanitize_text(company_address)
    }
    response = None
    try:
        response = requests.post(WP_SAVE_COMPANY_URL, json=post_data, headers=wp_headers, timeout=15)
        response.raise_for_status()
        res = response.json()
        if res.get("success"):
            logger.info(f"Successfully saved company {company_name}: Company ID {company_id}")
            return company_id, f"{WP_SITE_URL}/wp-content/uploads/companies.json"
        elif res.get("message") == "Company already exists":
            logger.info(f"Found existing company {company_name}: Company ID {company_id}")
            return company_id, f"{WP_SITE_URL}/wp-content/uploads/companies.json"
        else:
            logger.error(f"Failed to save company {company_name}: {res}")
            return None, None
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to save company {company_name}: {str(e)}, Status: {response.status_code if response else 'None'}, Response: {response.text if response else 'None'}")
        return None, None

def save_article_to_wordpress(index, job_data, company_id, auth_headers):
    if check_fetcher_status(auth_headers) != 'running':
        logger.info("Fetcher stopped before saving job")
        return None, None

    job_title = job_data.get("job_title", "")
    job_description = job_data.get("job_description", "")
    job_type = job_data.get("job_type", "")
    job_type = FRENCH_TO_ENGLISH_JOB_TYPE.get(job_type, job_type)
    job_type = JOB_TYPE_MAPPING.get(job_type, job_type)
    location = job_data.get("location", COUNTRY)
    job_url = job_data.get("job_url", "")
    company_name = job_data.get("company_name", "")
    company_logo = job_data.get("company_logo", "")
    environment = job_data.get("environment", "").lower()
    job_salary = job_data.get("job_salary", "")
    company_industry = job_data.get("company_industry", "")
    company_founded = job_data.get("company_founded", "")
    company_address = job_data.get("company_address", "")
    
    job_id = generate_job_id(job_title, company_name)
    
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
            media_response = requests.post(f"{WP_SITE_URL}/wp-json/wp/v2/media", headers=logo_headers, data=logo_response.content)
            media_response.raise_for_status()
            attachment_id = media_response.json().get("id", 0)
            logger.info(f"Uploaded logo for job {job_title}, Attachment ID: {attachment_id}")
        except Exception as e:
            logger.error(f"Failed to upload logo for job {job_title}: {str(e)}")

    post_data = {
        "job_id": job_id,
        "job_title": sanitize_text(job_title),
        "job_description": sanitize_text(job_description),
        "job_location": sanitize_text(location),
        "job_type": sanitize_text(job_type),
        "job_salary": sanitize_text(job_salary),
        "application": sanitize_text(application, is_url=('@' not in application)),
        "company_id": str(company_id) if company_id else "",
        "company_name": sanitize_text(company_name),
        "company_website": sanitize_text(job_data.get("company_website_url", ""), is_url=True),
        "company_logo": str(attachment_id) if attachment_id else "",
        "company_address": sanitize_text(company_address),
        "company_industry": sanitize_text(company_industry),
        "company_founded": sanitize_text(company_founded)
    }
    
    logger.info(f"Final job post payload for {job_title}: {json.dumps(post_data, indent=2)[:200]}...")
    
    try:
        response = requests.post(WP_SAVE_JOB_URL, json=post_data, headers=auth_headers, timeout=15)
        response.raise_for_status()
        res = response.json()
        if res.get("success"):
            logger.info(f"Successfully saved job {job_title}: Job ID {job_id}")
            return job_id, f"{WP_SITE_URL}/wp-content/uploads/jobs.json"
        elif res.get("message") == "Job already exists":
            logger.info(f"Found existing job {job_title}: Job ID {job_id}")
            return job_id, f"{WP_SITE_URL}/wp-content/uploads/jobs.json"
        else:
            logger.error(f"Failed to save job {job_title}: {res}")
            return None, None
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to save job {job_title}: {str(e)}, Status: {response.status_code if response else 'None'}, Response: {response.text if response else 'None'}")
        return None, None

def load_processed_ids():
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
    try:
        with open(PROCESSED_IDS_FILE, "a") as f:
            f.write(f"{job_id}\n")
        logger.info(f"Saved job ID {job_id} to {PROCESSED_IDS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save job ID {job_id} to {PROCESSED_IDS_FILE}: {str(e)}")

def load_last_page():
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
    try:
        with open(LAST_PAGE_FILE, "w") as f:
            f.write(str(page))
        logger.info(f"Saved last processed page: {page} to {LAST_PAGE_FILE}")
    except Exception as e:
        logger.error(f"Failed to save last page to {LAST_PAGE_FILE}: {str(e)}")

def crawl(auth_headers, processed_ids):
    if check_fetcher_status(auth_headers) != 'running':
        logger.info("Fetcher stopped by initial status check")
        print("Fetcher is not running. Exiting.")
        return

    success_count = 0
    failure_count = 0
    total_jobs = 0
    start_page = load_last_page()
    
    for i in range(start_page, 15):
        if check_fetcher_status(auth_headers) != 'running':
            logger.info("Fetcher stopped during page processing")
            print("Fetcher stopped by user. Exiting.")
            break

        url = f'https://www.linkedin.com/jobs/search?keywords={KEYWORD}&location={COUNTRY}&start={i * 25}'
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
                print("Login or CAPTCHA detected, stopping crawl")
                break
            soup = BeautifulSoup(response.text, 'html.parser')
            job_list = soup.select("#main-content > section > ul > li > div > a")
            urls = [a['href'] for a in job_list if a.get('href')]
            logger.info(f'Found {len(urls)} job URLs on page: {url}')
            
            for index, job_url in enumerate(urls):
                if check_fetcher_status(auth_headers) != 'running':
                    logger.info("Fetcher stopped during job processing")
                    print("Fetcher stopped by user. Exiting.")
                    break

                job_data = scrape_job_details(job_url, auth_headers)
                if not job_data:
                    logger.error(f"No data fetched for job: {job_url}")
                    print(f"Job (URL: {job_url}) failed to fetch: No data returned")
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
                    logger.info(f"Skipping already processed job: {job_id} ({job_title} at {company_name})")
                    print(f"Job '{job_title}' at {company_name} (ID: {job_id}) skipped - already processed.")
                    total_jobs += 1
                    continue
                
                if not company_name or company_name.lower() == "unknown":
                    logger.info(f"Skipping job with unknown company: {job_title} (ID: {job_id})")
                    print(f"Job '{job_title}' (ID: {job_id}) skipped - unknown company")
                    failure_count += 1
                    total_jobs += 1
                    continue
                
                total_jobs += 1
                
                company_id, company_url = save_company_to_wordpress(index, job_dict, auth_headers)
                if company_id is None:
                    failure_count += 1
                    continue

                job_post_id, job_post_url = save_article_to_wordpress(index, job_dict, company_id, auth_headers)
                if job_post_id is None:
                    failure_count += 1
                    continue
                
                processed_ids.add(job_id)
                save_processed_id(job_id)
                logger.info(f"Processed and saved job: {job_id} - {job_title} at {company_name}")
                print(f"Job '{job_title}' at {company_name} (ID: {job_id}) successfully posted to WordPress. Post ID: {job_post_id}, URL {job_post_url}")
                success_count += 1
            
            save_last_page(i)
        
        except Exception as e:
            logger.error(f'Error fetching job search page: {url} - {str(e)}')
            print(f"Error fetching page {url}: {str(e)}")
            failure_count += 1
    
    print("\n--- Summary ---")
    print(f"Total jobs processed: {total_jobs}")
    print(f"Successfully posted: {success_count}")
    print(f"Failed to post or fetch: {failure_count}")

def scrape_job_details(job_url, auth_headers):
    if check_fetcher_status(auth_headers) != 'running':
        logger.info("Fetcher stopped before fetching job details")
        return None

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
        logger.info(f'Fetched Job Title: {job_title}')

        company_logo = soup.select_one("#main-content > section.core-rail.mx-auto.papabear\:w-core-rail-width.mamabear\:max-w-\[790px\].babybear\:max-w-\[790px\] > div > section.top-card-layout.container-lined.overflow-hidden.babybear\:rounded-\[0px\] > div > a > img")
        company_logo = (company_logo.get('data-delayed-url') or company_logo.get('src') or '') if company_logo else ''
        logger.info(f'Fetched Company Logo URL: {company_logo}')

        company_name = soup.select_one(".topcard__org-name-link")
        company_name = company_name.get_text().strip() if company_name else ''
        logger.info(f'Fetched Company Name: {company_name}')

        company_url = soup.select_one(".topcard__org-name-link")
        company_url = company_url['href'] if company_url and company_url.get('href') else ''
        if company_url:
            company_url = re.sub(r'\?.*$', '', company_url)
            logger.info(f'Fetched Company URL: {company_url}')
        else:
            logger.info('No Company URL found')

        if check_fetcher_status(auth_headers) != 'running':
            logger.info("Fetcher stopped before fetching company details")
            return None

        location = soup.select_one(".topcard__flavor.topcard__flavor--bullet")
        location = location.get_text().strip() if location else COUNTRY
        location_parts = [part.strip() for part in location.split(',') if part.strip()]
        location = ', '.join(dict.fromkeys(location_parts))
        logger.info(f'Deduplicated location for {job_title}: {location}')

        environment = ''
        env_element = soup.select(".topcard__flavor--metadata")
        for elem in env_element:
            text = elem.get_text().strip().lower()
            if 'remote' in text or 'hybrid' in text or 'on-site' in text:
                environment = elem.get_text().strip()
                break
        logger.info(f'Fetched Environment: {environment}')

        level = soup.select_one(".description__job-criteria-list > li:nth-child(1) > span")
        level = level.get_text().strip() if level else ''
        logger.info(f'Fetched Level: {level}')

        job_type = soup.select_one(".description__job-criteria-list > li:nth-child(2) > span")
        job_type = job_type.get_text().strip() if job_type else ''
        job_type = FRENCH_TO_ENGLISH_JOB_TYPE.get(job_type, job_type)
        logger.info(f'Fetched Type: {job_type}')

        job_functions = soup.select_one(".description__job-criteria-list > li:nth-child(3) > span")
        job_functions = job_functions.get_text().strip() if job_functions else ''
        logger.info(f'Fetched Job Functions: {job_functions}')

        industries = soup.select_one(".description__job-criteria-list > li:nth-child(4) > span")
        industries = industries.get_text().strip() if industries else ''
        logger.info(f'Fetched Industries: {industries}')

        job_description = ''
        description_container = soup.select_one(".show-more-less-html__markup")
        if description_container:
            paragraphs = description_container.find_all(['p', 'li'], recursive=False)
            if paragraphs:
                seen = set()
                unique_paragraphs = []
                logger.debug(f"Raw paragraphs for {job_title}: {[sanitize_text(p.get_text().strip())[:50] for p in paragraphs if p.get_text().strip()]}")
                for p in paragraphs:
                    para = sanitize_text(p.get_text().strip())
                    if not para:
                        continue
                    norm_para = normalize_for_deduplication(para)
                    if norm_para and norm_para not in seen:
                        unique_paragraphs.append(para)
                        seen.add(norm_para)
                    elif norm_para:
                        logger.info(f"Removed duplicate paragraph in job description for {job_title}: {para[:50]}...")
                job_description = '\n\n'.join(unique_paragraphs)
            else:
                raw_text = description_container.get_text(separator='\n').strip()
                paragraphs = [para.strip() for para in raw_text.split('\n\n') if para.strip()]
                seen = set()
                unique_paragraphs = []
                logger.debug(f"Raw text paragraphs for {job_title}: {[sanitize_text(para)[:50] for para in paragraphs]}")
                for para in paragraphs:
                    para = sanitize_text(para)
                    if not para:
                        continue
                    norm_para = normalize_for_deduplication(para)
                    if norm_para and norm_para not in seen:
                        unique_paragraphs.append(para)
                        seen.add(norm_para)
                    elif norm_para:
                        logger.info(f"Removed duplicate paragraph in job description for {job_title}: {para[:50]}...")
                job_description = '\n\n'.join(unique_paragraphs)
            logger.info(f'Raw Job Description (length): {len(job_description)}')
            job_description = re.sub(r'(?i)(?:\s*Show\s+more\s*$|\s*Show\s+less\s*$)', '', job_description, flags=re.MULTILINE).strip()
            job_description = split_paragraphs(job_description, max_length=200)
            logger.info(f'Fetched Job Description (length): {len(job_description)}, Paragraphs: {len(job_description.split('\n\n'))}')
        else:
            logger.warning(f"No job description container found for {job_title}")

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
        logger.info(f'Fetched Application URL: {application_url}')

        resolved_application_info = ''
        resolved_application_url = ''
        final_application_email = description_application_info if description_application_info and '@' in description_application_info else ''
        final_application_url = description_application_url if description_application_url else ''

        if application_url:
            if check_fetcher_status(auth_headers) != 'running':
                logger.info("Fetcher stopped before following application URL")
                return None

            try:
                time.sleep(5)
                resp_app = session.get(application_url, headers=headers, timeout=15, allow_redirects=True)
                resolved_application_url = resp_app.url
                logger.info(f'Resolved Application URL: {resolved_application_url}')
                
                app_soup = BeautifulSoup(resp_app.text, 'html.parser')
                email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
                emails = re.findall(email_pattern, resp_app.text)
                if emails:
                    resolved_application_info = emails[0]
                    logger.info(f'Found email in application page: {resolved_application_info}')
                else:
                    links = app_soup.find_all('a', href=True)
                    for link in links:
                        href = link['href']
                        if 'apply' in href.lower() or 'careers' in href.lower() or 'jobs' in href.lower():
                            resolved_application_info = href
                            logger.info(f'Found application link in application page: {resolved_application_info}')
                            break

                if final_application_email and resolved_application_info and '@' in resolved_application_info:
                    final_application_email = final_application_email if final_application_email == resolved_application_info else final_application_email
                elif resolved_application_info and '@' in resolved_application_info:
                    final_application_email = final_application_email or resolved_application_info

                if description_application_url and resolved_application_url:
                    final_application_url = description_application_url if description_application_url == resolved_application_url else resolved_application_url
                elif resolved_application_url:
                    final_application_url = resolved_application_url

            except Exception as e:
                logger.error(f'Failed to follow application URL redirect: {str(e)}')
                error_str = str(e)
                external_url_match = re.search(r'host=\'([^\']+)\'', error_str)
                if external_url_match:
                    external_url = external_url_match.group(1)
                    final_application_url = f"https://{external_url}"
                    logger.info(f'Extracted external URL from error for application: {final_application_url}')
                else:
                    final_application_url = description_application_url if description_application_url else application_url or ''
                    logger.warning(f'No external URL found in error, using fallback: {final_application_url}')

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
            if check_fetcher_status(auth_headers) != 'running':
                logger.info("Fetcher stopped before fetching company page")
                return None

            logger.info(f'Fetching company page: {company_url}')
            try:
                company_response = session.get(company_url, headers=headers, timeout=15)
                company_response.raise_for_status()
                company_soup = BeautifulSoup(company_response.text, 'html.parser')

                company_details_elem = company_soup.select_one("p.about-us__description") or company_soup.select_one("section.core-section-container > div > p")
                company_details = company_details_elem.get_text().strip() if company_details_elem else ''
                logger.info(f'Fetched Company Details: {company_details[:100] + "..." if company_details else ""}')

                company_website_anchor = company_soup.select_one("dl > div:nth-child(1) > dd > a")
                company_website_url = company_website_anchor['href'] if company_website_anchor and company_website_anchor.get('href') else ''
                logger.info(f'Fetched Company Website URL: {company_website_url}')

                if 'linkedin.com/redir/redirect' in company_website_url:
                    parsed_url = urlparse(company_website_url)
                    query_params = parse_qs(parsed_url.query)
                    if 'url' in query_params:
                        company_website_url = unquote(query_params['url'][0])
                        logger.info(f'Extracted external company website from redirect: {company_website_url}')
                    else:
                        logger.warning(f'No "url" param in LinkedIn redirect for {company_name}')

                if company_website_url and 'linkedin.com' not in company_website_url:
                    if check_fetcher_status(auth_headers) != 'running':
                        logger.info("Fetcher stopped before resolving company website")
                        return None

                    try:
                        time.sleep(5)
                        resp_company_web = session.get(company_website_url, headers=headers, timeout=15, allow_redirects=True)
                        company_website_url = resp_company_web.url
                        logger.info(f'Resolved Company Website URL: {company_website_url}')
                    except Exception as e:
                        logger.error(f'Failed to resolve company website URL: {str(e)}')
                        error_str = str(e)
                        external_url_match = re.search(r'host=\'([^\']+)\'', error_str)
                        if external_url_match:
                            external_url = external_url_match.group(1)
                            company_website_url = f"https://{external_url}"
                            logger.info(f'Extracted external URL from error for company website: {company_website_url}')
                        else:
                            logger.warning(f'No external URL found in error for {company_name}')
                            company_website_url = ''
                else:
                    description_elem = company_soup.select_one("p.about-us__description")
                    if description_elem:
                        description_text = description_elem.get_text()
                        url_pattern = r'https?://(?!www\.linkedin\.com)[^\s]+'
                        urls = re.findall(url_pattern, description_text)
                        if urls:
                            company_website_url = urls[0]
                            logger.info(f'Found company website in description: {company_website_url}')
                            if check_fetcher_status(auth_headers) != 'running':
                                logger.info("Fetcher stopped before resolving company website from description")
                                return None
                            try:
                                time.sleep(5)
                                resp_company_web = session.get(company_website_url, headers=headers, timeout=15, allow_redirects=True)
                                company_website_url = resp_company_web.url
                                logger.info(f'Resolved Company Website URL from description: {company_website_url}')
                            except Exception as e:
                                logger.error(f'Failed to resolve company website URL from description: {str(e)}')
                                company_website_url = ''
                        else:
                            logger.warning(f'No valid company website URL found in description for {company_name}')
                            company_website_url = ''
                    else:
                        logger.warning(f'No company description found for {company_name}')
                        company_website_url = ''

                if company_website_url and 'linkedin.com' in company_website_url:
                    logger.warning(f'Skipping LinkedIn URL for company website: {company_website_url}')
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
                logger.info(f'Fetched Company Industry: {company_industry}')

                company_size = get_company_detail("Company size")
                logger.info(f'Fetched Company Size: {company_size}')

                company_headquarters = get_company_detail("Headquarters")
                logger.info(f'Fetched Company Headquarters: {company_headquarters}')

                company_type = get_company_detail("Type")
                logger.info(f'Fetched Company Type: {company_type}')

                company_founded = get_company_detail("Founded")
                logger.info(f'Fetched Company Founded: {company_founded}')

                company_specialties = get_company_detail("Specialties")
                logger.info(f'Fetched Company Specialties: {company_specialties}')

                company_address = company_soup.select_one("#address-0")
                company_address = company_address.get_text().strip() if company_address else company_headquarters
                logger.info(f'Fetched Company Address: {company_address}')
            except Exception as e:
                logger.error(f'Error fetching company page: {company_url} - {str(e)}')
        else:
            logger.info('No company URL, skipping company details fetch')

        row = [
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
        logger.info(f'Full fetched row for job: {str(row)[:200] + "..."}')
        return row

    except Exception as e:
        logger.error(f'Error in scrape_job_details for {job_url}: {str(e)}')
        return None

def main():
    if not all([WP_SITE_URL, WP_USERNAME, WP_APP_PASSWORD, COUNTRY]):
        logger.error("Missing required environment variables (WP_SITE_URL, WP_USERNAME, WP_APP_PASSWORD, COUNTRY). Exiting.")
        print("Missing required environment variables. Exiting.")
        exit(1)

    auth_string = f"{WP_USERNAME}:{WP_APP_PASSWORD}"
    auth = base64.b64encode(auth_string.encode()).decode()
    wp_headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json"
    }
    
    processed_ids = load_processed_ids()
    crawl(auth_headers=wp_headers, processed_ids=processed_ids)

if __name__ == "__main__":
    main()
