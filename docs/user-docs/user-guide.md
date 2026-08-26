# User Guide

This guide will help you understand how to use **RADIS** for managing and searching radiology reports.

## How RADIS Works

RADIS acts as a centralized repository for radiology reports, providing powerful search capabilities and organizational tools:

1. **Reports are stored** with structured metadata (patient info, study details, modalities, etc.)
2. **You search** using natural language queries with optional filters
3. **RADIS processes** your query with full-text search, combined with semantic (meaning-based) search when your administrator has configured it
4. **You receive** ranked results matching your criteria
5. **You organize** reports into collections, add notes, and subscribe to searches
6. **AI assists you** by labeling reports, extracting structured data, and answering questions about a report

## Functionalities Overview

When you log into RADIS, you'll see the main/homepage with several sections:

- **Search**: Search for reports using text queries and filters
- **Collections**: Organize reports into custom collections
- **Subscriptions**: Get notified about new reports that match your criteria, with AI-based filtering and data extraction
- **Notes**: View and manage notes you've added to reports
- **Extractions**: Extract structured data from many reports at once using AI
- **Chats**: Interactive chat interface with AI

## Main Features

### 1. Search Reports

The search feature is the core of RADIS, allowing you to find relevant reports quickly:

1. Navigate to the "Search" section
2. Enter your search query in the search box
3. Apply filters as needed on the right-hand side
4. Review the search results

#### Search Syntax

- **Case-insensitive**: Search terms are not case-sensitive
- **AND queries**: All terms must match (implicit AND between terms, or write `AND` explicitly)
- **OR operator**: Use capital `OR` between terms (e.g., `fracture OR lesion`)
- **NOT operator**: Use capital `NOT` to exclude a term (e.g., `pneumonia NOT metastasis`)
- **Phrases**: Use quotes for exact matches (e.g., `"lung cancer"`)
- **Grouping**: Use parentheses to combine operators (e.g., `(fracture OR lesion) NOT metastasis`)

Operators must be written in capital letters; `or`, `and`, and `not` in lowercase are treated as ordinary search terms. A minus sign in front of a term does not exclude it, and `field:value` syntax is not supported — use the filters instead.

If your query contains mistakes such as unbalanced quotes or parentheses, RADIS repairs it and shows a "Fixed invalid query" notice above the results with the query it actually ran.

#### Semantic Search

When your administrator has configured an embedding model, every search combines classic keyword matching with semantic search, which also finds reports that describe the same thing in different words. This happens automatically; there is no switch to turn it on or off. Each result shows the scores that determined its ranking.

#### Available Filters

- **Language**: Filter by report language. The default "All" searches reports in every language
- **Modalities**: Filter by imaging modality (CT, MRI, X-ray, etc.)
- **Labels**: Only show reports that carry at least one of the selected labels (see [Labels](#2-labels)). This filter only appears when labels have been set up
- **Study Date**: Filter by date range (from/till)
- **Study Description**: Filter by study description (partial match)
- **Patient Sex**: All, Male, or Female
- **Age Range**: Filter by patient age using the range slider (in steps of 10 years)

Use "Reset filters" to clear all filters at once.

#### Search Results

Each result shows the patient age and sex, modalities, and study description, followed by a highlighted excerpt of the report. Click "Show full report" to read the entire text. Each result panel has buttons to open the report details, add the report to a collection, add a note, start a chat about the report, and open the study in your PACS viewer.

### 2. Labels

Administrators can define labels (e.g. "Pulmonary nodule" or "Fracture") that RADIS assigns to reports automatically using AI. Each label is classified as Present, Likely, or Possible when the report supports it; reports where the label is absent or not mentioned carry no badge.

- Labels appear as badges on the report detail page, grouped by their label group. Hover over a badge to see whether the finding is Present, Likely, or Possible
- A greyed-out badge means the report or the label definition changed since the label was assigned; it will be refreshed by the next labeling run
- Use the **Labels** filter in the search to find all reports carrying a label. Selecting several labels finds reports that carry any of them

Labels cannot be edited by users. If a label seems wrong or missing, contact your administrator.

### 3. Collections

Collections allow you to organize reports for easy access:

1. Go to the "Collections" section to view your collections
2. Click "Create Collection" to create a new collection
3. Add reports to collections by clicking the collection button on report panels
4. View collection contents by clicking on the collection name

Collections are useful for:

- Grouping cases for research or review
- Creating teaching file collections
- Organizing reports by project or study

### 4. Notes

Add personal notes to reports for additional context:

1. Open a report detail view
2. Click the notes button to add or edit notes
3. View all your notes in the "Notes" section
4. Notes are private to your user account

### 5. Subscriptions

Set up subscriptions to be notified when new reports match your criteria. RADIS checks for new reports every hour. Optionally, an AI model screens each new report with your questions and extracts data from it:

1. Navigate to the "Subscriptions" section
2. Click "Add Subscription"
3. Enter a **Name** for the subscription
4. Narrow down the reports with the filters on the right: Patient ID, Language, Modalities, Study Description, Patient Sex, and Age Range
5. Optionally add up to three **Filter Questions**. Each is a yes/no question about the report (e.g. "Does the report describe a new pulmonary nodule?") together with the answer that should be accepted (Yes or No). A report only enters your inbox when the AI's answer to every question matches
6. Optionally add up to ten **Extraction Fields** to have data extracted from every matching report. Each field has a Name, a Description telling the AI what to extract, and an Output Type (Text, Numeric, Boolean, or Selection with a fixed list of options). Use the `[ ]` toggle next to the type to collect multiple values per report
7. Check "Notify me via mail of new reports" if you want an email whenever a refresh finds new reports
8. Save the subscription

Only reports that arrive (or are updated) after the subscription was created are considered; existing reports are not searched retroactively.

#### Inbox

The subscription list shows how many reports each subscription has collected, with a badge for reports you have not looked at yet. Click the count to open the inbox:

- Each matched report is shown with its header, a preview of the text, and — if you defined extraction fields — the extracted values
- Sort by arrival or study date, and narrow the list with the filters on the side (Patient ID, Study Description, Study Date, Modalities)
- Click "Download Extractions as CSV" to export the extracted values of the listed reports as a spreadsheet
- Opening the inbox marks its reports as seen

Use the "Edit" and "Delete" buttons on the subscription page to change or remove a subscription. Changes apply to future refreshes only; reports already in the inbox stay there.

### 6. Extractions

Extraction jobs let you pull structured data out of many reports at once. Creating a job takes three steps:

**Step 1 – Define fields.** Add one to five fields that you want extracted from each report. For each field, specify:

- **Name**: A short column name for the results
- **Description**: What the AI should extract (e.g. "Largest diameter of the primary tumor in mm")
- **Output Type**: Text, Numeric, Boolean, or Selection. For Selection, enter the allowed options (up to seven)
- The **`[ ]` toggle** next to the type switches the field to an array, so multiple values of that type are returned per report (e.g. a list of all affected vertebrae)

**Step 2 – Search query.** Enter a job title. RADIS generates a search query from your field descriptions, which you can edit or replace. Apply filters (Language, Modalities, Study Date, Study Description, Patient Sex, Age Range) to narrow down the reports. A live counter shows how many reports the job will process; a job may cover at most 25,000 reports. Click "Preview Search Results" to open the matching reports in the regular search view in a new tab.

**Step 3 – Review & submit.** Check the summary, optionally tick "Notify me via Email when job is finished", and click "Create Extraction Job".

Jobs of regular users start in the "Unverified" state and are queued once an administrator has verified them. On the job page you can follow the progress of the individual tasks and, once results come in:

- Click **View Results** to browse the extracted values in a table with one column per field
- Click **Download CSV** to export the table
- Use the control panel to cancel a running job, resume a canceled one, or retry failed tasks

The "Extractions" menu item opens the wizard directly; click "Previous Jobs" in the wizard (or "Job List" on a job page) to see all your jobs.

### 7. Chats (AI Assistant)

If enabled, the Chats feature provides an interactive AI assistant:

- Start a chat from a report panel to ask questions about that report in natural language
- Get contextual answers based on report content
- Useful for exploring report data interactively

## RADIS Client

RADIS Client is a Python library that provides programmatic access to RADIS features without using the web interface.

To use the RADIS Client, you must have an API token provided by an administrator. For instructions on generating an API token, refer to the [Admin Guide](admin-guide.md).
