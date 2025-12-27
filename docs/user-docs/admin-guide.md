# Admin Guide

The Admin Guide is intended for system administrators and technical staff responsible for configuring and maintaining RADIS (Radiology Report Archive and Discovery System).

## Installation

```terminal
Clone the repository: git clone https://github.com/openradx/radis.git
cd radis
uv sync
cp ./example.env ./.env  # copy example environment to .env
uv run cli stack-deploy  # builds and starts Docker containers
```

## Updating RADIS

Follow these steps to safely update your RADIS installation:

1. **Verify no active jobs**: Navigate to Django Admin → **Analysis Jobs** and confirm no extractions or subscriptions are running
2. **Enable maintenance mode**: In Django Admin, navigate to **Common** → **Project Settings** and check the "Maintenance mode" checkbox, then save
3. Navigate to Production folder
4. **Backup database**: Run `uv run cli db-backup` to create a database backup
5. **Remove stack**: Run `uv run cli stack-rm` to remove all Docker containers and services
6. **Pull latest changes**: Run `git pull origin main` to fetch the latest code updates
7. **Update environment**: Compare `example.env` with your `.env` file and add any new environment variables or update changed values
8. **Pull Docker images**: Run `uv run cli compose-pull` to download the latest Docker images
9. **Deploy stack**: Run `uv run cli stack-deploy` to rebuild and start all services with the updated code
10. **Disable maintenance mode**: In Django Admin, navigate to **Common** → **Project Settings** and uncheck the "Maintenance mode" checkbox, then save

## User and Group Management

Administrators can create users by navigating to the Django Admin section. Alternatively, users can self-register, after which an administrator must approve and activate their account.

RADIS uses a group-based permission system:

- **Groups** define access to specific reports based on organizational structure
- **Users** are assigned to one or more groups to inherit their permissions
- **Active Group** determines which reports a user can currently access

### Creating and Managing Groups

1. **Access Django Admin**:

   - Log in as a staff user
   - Go to **Admin Section** → **Django Admin** (available at `/django-admin/` URL path)

2. **Create/Edit Groups**:

   - Navigate to **Authentication and Authorization** → **Groups**
   - Click "Add Group" or edit an existing group
   - Give the group a **Name** (e.g., "Radiology Department", "Research Team", "Oncology")

3. **Assign Permissions**:

   - In the group form, you'll see **Available permissions** and **Chosen permissions**
   - Select the permissions you want from the available list:
     - `extractions | extraction job | Can process urgently`
     - `subscriptions | subscription | Can process urgently`
     - Plus other RADIS-specific permissions for viewing/managing extractions, subscriptions, and collections
   - Move them to **Chosen permissions**

4. **Add Users to Group**:

   - In the **Users** section, select users from **Available users**
   - Move them to **Chosen users**
   - Click **Save** to apply all changes

### Active Group

Each user has an **active group** that determines which reports they can currently access:

- Users can switch between their assigned groups
- Only reports associated with the active group are visible in searches and collections
- This ensures proper data isolation between different departments or projects

## Report Management

### Managing Modalities

Define DICOM modalities for report filtering:

1. **Access Django Admin**: Navigate to **Reports** → **Modalities**
2. **Add Modality**:
   - Click **Add Modality**
   - Enter **Code** (e.g., "CT", "MR", "US", "XR")
   - Enter **Name** (e.g., "Computed Tomography", "Magnetic Resonance")
   - Save

### Report Import and Management

Administrators can import reports programmatically via the RADIS API or using the RADIS Client library. See the **RADIS Client** section below for details.

## AI/LLM Configuration

RADIS uses Large Language Models (LLMs) for AI-powered features including extractions and subscription filtering.

### Extraction Jobs

Administrators can monitor and manage extraction jobs where AI analyzes reports to extract structured data.

### Managing Extractions

1. **Access Django Admin**: Navigate to **Extractions** → **Extraction Jobs**
2. **Monitor Status**: View jobs by status (Preparing, Pending, In Progress, Success, Failure, Canceled)
3. **View Details**: Click on a job to see:
   - Owner and group
   - Query and filters
   - Output field definitions
   - Processing statistics
   - Results data

### Granting Urgent Priority Permission

Users with urgent priority permission can skip the queue:

1. Navigate to **Authentication and Authorization** → **Groups**
2. Edit the desired group
3. Add permission: `extractions | extraction job | Can process urgently`
4. Save

## Subscription Management

Administrators can oversee automated report subscriptions and notifications.

### Managing Subscriptions

1. **Access Django Admin**: Navigate to **Subscriptions** → **Subscriptions**
2. **View All Subscriptions**: See all active subscriptions across users
3. **Configure Periodic Launcher**:
   - Default: Runs every minute to check for new matching reports
   - Configured in Django settings: `SUBSCRIPTION_CRON`

### Subscription Jobs

Monitor subscription refresh jobs:

1. Navigate to **Subscriptions** → **Subscription Jobs**
2. View job status and matched reports
3. Check email notification delivery

### Granting Urgent Priority Permission

1. Navigate to **Authentication and Authorization** → **Groups**
2. Edit the desired group
3. Add permission: `subscriptions | subscription | Can process urgently`
4. Save

## Background Workers

RADIS uses Procrastinate for distributed task processing with two worker types:

## System Announcements

System administrators can inform users about important updates, maintenance schedules, or system changes through the announcement feature.

### Creating Announcements

1. **Access Admin Interface**: Navigate to the Django admin interface (typically accessible at `/django-admin/`)
2. **Find Project Settings**: Go to the "Common" section and select "Project settings"
3. **Edit Announcement**: In the Project Settings form, locate the "Announcement" field
4. **Enter Message**: Type your announcement message. HTML formatting is supported for rich text display
5. **Save Changes**: Click "Save" to publish the announcement

### Announcement Display

- Announcements appear prominently on the main/home page
- All logged-in users will see the announcement when they access RADIS

#### Example Announcements

**Maintenance Notice:**

```html
<strong>Scheduled Maintenance:</strong> RADIS will be offline for maintenance on
<strong>March 15, 2024 from 2:00 AM to 4:00 AM UTC</strong>. Please plan your
extractions and subscriptions accordingly.
```

**New Feature Announcement:**

```html
<strong>New Feature Available:</strong> You can now create custom extraction
jobs with multiple output fields. Check out the user guide for more details.
```

## Storage and Disk Space Monitoring

RADIS includes disk space monitoring:

1. **Configure Monitoring**: Set up periodic checks in Django Admin → **Procrastinate** → **Periodic Deferrer**
2. **Alert Thresholds**: Configure in Django settings
3. **View Alerts**: Check Django Admin for disk space warnings

## RADIS Client

RADIS Client is a Python library for programmatic access to RADIS features without using the web interface.

### Creating API Tokens

To create an API token for programmatic access:

1. **Log in** to RADIS with your administrator account
2. **Navigate to Token Authentication**:
   - Go to **Django Admin** → **Token Authentication** → **Tokens**
3. **Add Token**:
   - Click **Add Token**
   - **Owner**: Select the user who will own the token
   - **Token hashed**: Enter the hashed token value
   - **Description**: Add a description (e.g., "API access for report import")
   - **Expires**: Set expiration date or leave empty for no expiration
   - Click **Save**
