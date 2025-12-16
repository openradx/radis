# User Guide

This guide will help you understand how to use **RADIS** for managing and searching radiology reports.

## How RADIS Works

RADIS acts as a centralized repository for radiology reports, providing powerful search capabilities and organizational tools:

1. **Reports are stored** with structured metadata (patient info, study details, modalities, etc.)
2. **You search** using natural language queries with optional filters
3. **RADIS processes** your query using advanced search algorithms (BM25, semantic, or hybrid search)
4. **You receive** ranked results matching your criteria
5. **You organize** reports into collections, add notes, and subscribe to searches

## Dashboard Overview

When you log into RADIS, you'll see the main dashboard with several sections:

- **Search**: Search for reports using text queries and filters
- **Collections**: Organize reports into custom collections
- **Subscriptions**: Set up notifications for new matching reports
- **Notes**: View and manage notes you've added to reports
- **Extractions**: AI-powered analysis and filtering (if enabled)
- **Chats**: Interactive chat interface with AI (if enabled)

## Main Features

### 1. Search Reports

The search feature is the core of RADIS, allowing you to find relevant reports quickly:

1. Navigate to the "Search" section
2. Enter your search query in the search box
3. Apply filters as needed (language, modality, date range, patient demographics)
4. Review the search results

#### Search Syntax

- **Case-insensitive**: Search terms are not case-sensitive
- **AND queries**: All terms must match (implicit AND between terms)
- **OR operator**: Use capital OR between terms (e.g., "fracture OR lesion")
- **Phrases**: Use quotes for exact matches (e.g., "lung cancer")
- **Exclusion**: Use minus sign to exclude terms (e.g., "-metastasis")

#### Available Filters

- **Language**: Filter by report language
- **Modalities**: Filter by imaging modality (CT, MRI, X-ray, etc.)
- **Study Date**: Filter by date range
- **Study Description**: Filter by study description
- **Patient Sex**: Filter by patient sex (M/F/U)
- **Patient Age**: Filter by age range
- **Patient ID**: Search for specific patient ID

### 2. Collections

Collections allow you to organize reports for easy access:

1. Go to the "Collections" section to view your collections
2. Click "Create Collection" to create a new collection
3. Add reports to collections by clicking the collection button on report panels
4. View collection contents by clicking on the collection name

Collections are useful for:

- Grouping cases for research or review
- Creating teaching file collections
- Organizing reports by project or study

### 3. Notes

Add personal notes to reports for additional context:

1. Open a report detail view
2. Click the notes button to add or edit notes
3. View all your notes in the "Notes" section
4. Notes are private to your user account

### 4. Subscriptions

Set up subscriptions to be notified when new reports match your criteria:

1. Navigate to the "Subscriptions" section
2. Click "Create Subscription" to set up a new subscription
3. Configure search criteria and filters
4. Choose to receive email notifications
5. New matching reports will be tracked and you'll receive notifications

#### Subscription Options

- **Name**: Descriptive name for your subscription
- **Query**: Search query to match against new reports
- **Filters**: Same filters available in regular search
- **Email Notifications**: Receive email when new matches are found

### 5. Extractions (AI-Powered Analysis)

- Create extraction jobs to analyze multiple reports
- Ask specific questions about report content
- Get structured answers extracted by AI
- Useful for research and quality assurance

### 6. Chats (AI Assistant)

If enabled, the Chats feature provides an interactive AI assistant:

- Ask questions about reports in natural language
- Get contextual answers based on report content
- Useful for exploring report data interactively

## User Interface Elements

### Report Panel

Each report in search results displays:

- Patient demographics (age, sex)
- Study information (modality, date, description)
- Report summary or excerpt
- Action buttons (collections, notes, chats, PACS link)

### Search Results

Search results show:

- Total number of matching reports
- Relevance-ranked list of reports
- Pagination controls for large result sets

## Administrator Features

### System Announcements

System administrators can create announcements that appear on the home page:

1. Access the Django admin interface at `/django-admin/`
2. Navigate to "Core" > "Project Settings"
3. Edit the "Announcement" field
4. Announcements support HTML formatting

Announcements are useful for:

- Notifying users about maintenance windows
- Announcing new features or changes
- Sharing important system updates

## Troubleshooting

### No Search Results

- Check your active group - you can only see reports in your group
- Verify filters are not too restrictive
- Try broader search terms
- Contact your administrator if reports should exist

### Cannot Create Collections

- Verify you have the necessary permissions
- Check if you've reached any collection limits
- Contact your administrator for assistance
