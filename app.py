# Version 2.0: Transitioned to 2.0 versioning, restarted version history for a clean slate.
from flask import Flask, request, render_template, jsonify
import requests
import PyPDF2
import io
import logging
import spacy
from pdf2image import convert_from_bytes
import pytesseract
import os
import shutil

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load spaCy model for NLP
nlp = spacy.load("en_core_web_sm")

# Define all possible base URLs for JFK file releases
BASE_URLS = {
    "2017-2018": "https://www.archives.gov/files/research/jfk/releases/",
    "2022": "https://www.archives.gov/files/research/jfk/releases/2022/",
    "2023": "https://www.archives.gov/files/research/jfk/releases/2023/",
    "2025": "https://www.archives.gov/files/research/jfk/releases/2025/0318/"
}

# Path to Poppler binaries (update this to match your new installation)
POPPLER_PATH = r"C:\Program Files\poppler\poppler-23.11.0\Library\bin"

# Path to Tesseract binary (update this to match your installation)
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Verify Poppler and Tesseract availability
def check_ocr_dependencies():
    # Check Poppler
    if not os.path.exists(POPPLER_PATH):
        logger.error(f"Poppler directory not found at {POPPLER_PATH}")
        return False, f"Poppler not found at {POPPLER_PATH}. Please install Poppler and update POPPLER_PATH."
    
    pdftoppm_path = os.path.join(POPPLER_PATH, "pdftoppm.exe")
    if not os.path.exists(pdftoppm_path):
        logger.error(f"pdftoppm.exe not found at {pdftoppm_path}")
        return False, f"pdftoppm.exe not found at {pdftoppm_path}. Please reinstall Poppler."

    # Check Tesseract
    if not os.path.exists(TESSERACT_PATH):
        logger.error(f"Tesseract executable not found at {TESSERACT_PATH}")
        return False, f"Tesseract not found at {TESSERACT_PATH}. Please install Tesseract and update TESSERACT_PATH."

    # Verify Tesseract is accessible
    if not shutil.which("tesseract"):
        logger.error("Tesseract not found in PATH")
        return False, "Tesseract not found in PATH. Please add Tesseract to your PATH or update TESSERACT_PATH."

    # Set pytesseract's path to Tesseract
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

    return True, None

APP_VERSION = "2.0"  # Updated to 2.0 versioning

# Check OCR dependencies on startup
dependencies_ok, dependency_error = check_ocr_dependencies()
if not dependencies_ok:
    logger.error(f"OCR dependencies check failed: {dependency_error}")
    # Instead of raising an error, we'll allow the app to start but disable OCR
    OCR_AVAILABLE = False
    logger.warning("OCR is disabled due to missing dependencies. Document Insights will be limited.")
else:
    OCR_AVAILABLE = True

@app.route("/", methods=["GET", "POST"])
def index():
    pdf_urls = []  # List to store all found URLs
    error = ""
    file_name = ""
    selected_pdf_url = ""  # To store the URL of the PDF to display initially
    selected_release_year = ""  # To store the release year of the selected PDF
    insights = {}  # To store the contextual analysis (entities and topics)

    if request.method == "POST":
        file_name = request.form["file_name"]
        file_name_with_extension = file_name + ".pdf"

        # Search for the file in all specified paths
        for release_year, base_url in BASE_URLS.items():
            url = base_url + file_name_with_extension
            logger.debug(f"Checking URL: {url}")
            try:
                response = requests.head(url, timeout=5)
                response.raise_for_status()
                logger.debug(f"Found file in {release_year} release: {url}")
                pdf_urls.append((release_year, url))  # Store the release year and URL
            except requests.exceptions.RequestException as e:
                logger.debug(f"Failed to find {url} in {release_year} release: {str(e)}")
                continue

        # If no files are found, set an error message
        if not pdf_urls:
            error = f"Error: Could not find the file '{file_name_with_extension}' in any release path. Please check the file name and try again."
            logger.error(f"No files found for {file_name_with_extension}")
        # If exactly one file is found, display it directly and fetch insights
        elif len(pdf_urls) == 1:
            selected_release_year, selected_pdf_url = pdf_urls[0]  # Unpack the release year and URL
            # Fetch contextual analysis (entities and topics) for the single file
            insights = fetch_insights(selected_pdf_url)
            logger.debug(f"Insights for single file {selected_pdf_url}: {insights}")

    return render_template(
        "index.html",
        pdf_urls=pdf_urls,
        error=error,
        file_name=file_name,
        selected_pdf_url=selected_pdf_url,
        selected_release_year=selected_release_year,
        insights=insights,
        app_version=APP_VERSION
    )

@app.route("/get_insights", methods=["GET"])
def get_insights():
    pdf_url = request.args.get("pdf_url")
    if not pdf_url:
        logger.error("No PDF URL provided in get_insights request")
        return jsonify({"error": "No PDF URL provided"}), 400

    insights = fetch_insights(pdf_url)
    logger.debug(f"Insights fetched: {insights}")
    return jsonify(insights)

def extract_entities_and_topics(text):
    """
    Extract key entities and topics from the given text using spaCy.
    """
    doc = nlp(text)
    
    # Extract entities (people, organizations, locations)
    entities = []
    for ent in doc.ents:
        if ent.label_ in ["PERSON", "ORG", "GPE"]:  # People, Organizations, Locations
            entities.append(f"{ent.text} ({ent.label_})")
    entities = list(dict.fromkeys(entities))[:3]  # Remove duplicates, limit to 3

    # Extract topics (noun chunks as a proxy for topics)
    topics = []
    for chunk in doc.noun_chunks:
        if chunk.text.lower() not in ["the", "a", "an"] and len(chunk.text) > 3:  # Filter out short or generic chunks
            topics.append(chunk.text)
    topics = list(dict.fromkeys(topics))[:3]  # Remove duplicates, limit to 3

    return entities, topics

def fetch_insights(pdf_url):
    """
    Fetch insights (key entities and topics) from the PDF using PyPDF2, pdf2image, and pytesseract for OCR.
    """
    try:
        # Download the PDF
        logger.debug(f"Downloading PDF from {pdf_url} for insights extraction")
        response = requests.get(pdf_url, timeout=10)
        response.raise_for_status()
        logger.debug(f"PDF downloaded successfully, size: {len(response.content)} bytes")

        # First, try to extract text directly using PyPDF2 (in case the PDF has a text layer)
        pdf_file = io.BytesIO(response.content)
        reader = PyPDF2.PdfReader(pdf_file)
        text = ""
        for page in reader.pages:
            page_text = page.extract_text() or ""
            text += page_text + " "

        # If no text is extracted and OCR is available, use OCR
        if not text.strip() and OCR_AVAILABLE:
            logger.debug("No text extracted with PyPDF2, falling back to OCR")
            # Convert PDF pages to images with explicit poppler_path
            images = convert_from_bytes(response.content, dpi=300, poppler_path=POPPLER_PATH, first_page=1, last_page=5)  # Limit to first 5 pages for performance
            for image in images:
                # Use pytesseract to extract text from the image
                page_text = pytesseract.image_to_string(image)
                text += page_text + " "

        # If still no text, return an error
        if not text.strip():
            if not OCR_AVAILABLE:
                logger.error("No text could be extracted from the PDF, and OCR is disabled due to missing dependencies")
                return {"error": "No text could be extracted from the PDF. OCR is disabled due to missing dependencies: " + dependency_error}
            logger.error("No text could be extracted from the PDF")
            return {"error": "No text could be extracted from the PDF"}

        # Extract entities and topics from the text
        entities, topics = extract_entities_and_topics(text)

        insights = {
            "Key Entities": entities,
            "Key Topics": topics
        }

        logger.info(f"Successfully extracted insights from PDF for {pdf_url}")
        return insights
    except Exception as e:
        logger.error(f"Error fetching insights from PDF for {pdf_url}: {str(e)}")
        return {"error": f"Could not fetch insights: {str(e)}"}

if __name__ == "__main__":
    app.run(debug=True)