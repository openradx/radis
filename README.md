# RADIS

## About

RADIS (Radiology Report Archive and Discovery System) is an innovative open-source web application developed by our team to enhance the management, retrieval, and analysis of radiology reports within hospital infrastructures.

> [!IMPORTANT]
> RADIS is currently in an early phase of development. While we are actively building and refining its features, users should anticipate ongoing updates and potential breaking changes as the platform evolves. We appreciate your understanding and welcome feedback to help us shape the future of RADIS.

## Features

- **Intuitive Web Interface**: Simplified access to radiology reports stored in the application database through a user-friendly web portal.
- **Advanced Text Search**: Robust search functionality combining semantic analysis and traditional keyword-based methods for precise report retrieval.
- **Seamless PACS Viewer Integration**: Direct access to PACS viewers with deep linking to relevant studies, leveraging the viewer's capabilities for a smooth workflow.
- **AI-Powered Search and Filtering**: Integration of large language models (LLMs) to enhance report discovery and filter options based on contextual understanding.
- **Bookmarking and Collection Management**: Organize reports into customizable collections with an intuitive bookmarking service for quick access and review.
- **Custom Report Notes**: Allow users to append personalized notes to reports for additional context or annotations.
- **Smart Notification System**: Subscription service that notifies users of new reports matching specific criteria, ensuring timely updates.

## Planned

- **Automated Report Classification and Organization**: Leverage LLMs to intelligently classify, tag, and organize reports based on content and metadata.
- **Developer-Friendly API Access**: Provide programmatic access to application features through a comprehensive API, with an optional Python client for seamless integration into workflows.
- **Report Quality Assurance**: Tools to review and assess reports for consistency, completeness, and content accuracy, ensuring high-quality documentation.

## API Client

[RADIS Client](https://github.com/openradx/radis-client) is a Python library to search for reports on RADIS in a programmatic way. It also allows admins to feed new reports to RADIS.

## Screenshots

Upcoming ...

## Architectural overview

RADIS employs a sophisticated multi-container architecture, optimized for local deployment using Docker Swarm mode—a feature included with all Docker installations. This local-first approach ensures compliance with the strict data security requirements inherent in hospital and research environments where sensitive patient or research data is managed. By leveraging Docker Swarm, RADIS offers seamless scalability, allowing services to be easily adjusted to meet the specific computational demands of the deployment site. To simplify the setup process, RADIS provides intuitive deployment scripts, ensuring accessibility for users with varying levels of technical expertise.

The RADIS web service is built on the robust Django Python framework, with data securely stored in a PostgreSQL database. By default, RADIS harnesses PostgreSQL’s powerful full-text search capabilities, enhanced by the pg_search and pg_vector extensions, to deliver hybrid search functionality. Its modular architecture enables effortless integration with other advanced text and vector search systems, such as Vespa or ElasticSearch, through easily implementable plugins.

To deliver a dynamic and responsive web interface, RADIS integrates modern JavaScript libraries HTMX and Alpine.js. For resource-intensive operations—such as batch evaluations or filtering large datasets—RADIS relies on Procrastinate, a Python-based distributed task processing library. This ensures long-running tasks are executed efficiently in the background, minimizing disruptions to user workflows while maximizing system performance.

RADIS’s design philosophy prioritizes security, flexibility, and user-friendly deployment, making it an ideal solution for managing sensitive data in high-demand environments.

## Disclaimer

RADIS is intended for research purposes only and is not a certified medical device. It should not be used for clinical diagnostics, treatment, or any medical applications. Use this software at your own risk. The developers and contributors are not liable for any outcomes resulting from its use.

## License

AGPLv3
