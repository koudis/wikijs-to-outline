#!/usr/bin/env python3
"""
Complete WikiJS GraphQL Exporter

This script exports the entire WikiJS wiki using GraphQL API with proper error handling,
authentication methods, and creates the same directory structure as git export.

Usage:
    python wikijs_graphql_complete_exporter.py --wiki-url https://wiki.bringauto.com --token YOUR_TOKEN --output-dir ./wikijs-complete-export
"""

import argparse
import requests
import json
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Any, Set
import time
import re
import urllib.parse
from datetime import datetime

class WikiJSGraphQLExporter:
    def __init__(self, wiki_url: str, api_token: str, output_dir: str, assets_only: bool = False):
        self.wiki_url = wiki_url.rstrip('/')
        self.api_token = api_token
        self.output_dir = Path(output_dir)
        self.assets_only = assets_only
        self.session = requests.Session()
        
        # Multiple authentication methods to try
        self.auth_headers = [
            {'Authorization': f'Bearer {api_token}'},
            {'Authorization': f'Token {api_token}'},
            {'X-API-Key': api_token},
            {'Cookie': f'jwt={api_token}'}
        ]
        
        # Common User-Agent to avoid blocking
        self.session.headers.update({
            'User-Agent': 'WikiJS-Exporter/1.0 (API Client)',
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        })
        
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Track what we've downloaded
        self.downloaded_assets: Set[str] = set()
        self.successfully_downloaded: Set[str] = set()  # Track actually downloaded files
        self.failed_downloads: Dict[str, List[str]] = {}  # asset_path -> list of reasons
        self.asset_to_pages: Dict[str, List[str]] = {}  # asset_path -> list of page paths that reference it
        self.exported_pages: List[Dict] = []
        
        
        print(f"WikiJS GraphQL Exporter initialized")
        print(f"Wiki URL: {self.wiki_url}")
        print(f"Output: {self.output_dir}")
    
    def test_graphql_connection(self) -> bool:
        """Test GraphQL connection with different authentication methods"""
        print("\nTesting GraphQL API connection...")
        
        # Simple introspection query to test connection
        test_query = """
        query TestConnection {
          __schema {
            queryType {
              name
            }
          }
        }
        """
        
        graphql_endpoints = [
            f"{self.wiki_url}/graphql",
            f"{self.wiki_url}/api/graphql",
            f"{self.wiki_url}/gql"
        ]
        
        for endpoint in graphql_endpoints:
            print(f"Testing endpoint: {endpoint}")
            
            for i, auth_header in enumerate(self.auth_headers):
                try:
                    headers = {**self.session.headers, **auth_header}
                    
                    response = requests.post(
                        endpoint,
                        json={'query': test_query},
                        headers=headers,
                        timeout=10
                    )
                    
                    print(f"  Auth method {i+1}: Status {response.status_code}")
                    
                    if response.status_code == 200:
                        result = response.json()
                        if 'data' in result and '__schema' in result['data']:
                            print(f"  ✓ Connection successful with auth method {i+1}")
                            # Set working configuration
                            self.graphql_url = endpoint
                            self.session.headers.update(auth_header)
                            return True
                    elif response.status_code == 403:
                        print(f"    403 Forbidden - {response.text[:100]}")
                    elif response.status_code == 401:
                        print(f"    401 Unauthorized")
                    else:
                        print(f"    Error: {response.text[:100]}")
                        
                except Exception as e:
                    print(f"    Exception: {e}")
        
        print("✗ All GraphQL connection attempts failed")
        return False
    
    def get_full_schema(self) -> Dict:
        """Get the complete GraphQL schema to understand available operations"""
        schema_query = """
        query IntrospectionQuery {
          __schema {
            queryType {
              fields {
                name
                description
                args {
                  name
                  description
                  type {
                    name
                    kind
                    ofType {
                      name
                      kind
                    }
                  }
                }
                type {
                  name
                  kind
                  fields {
                    name
                    type {
                      name
                      kind
                    }
                  }
                }
              }
            }
            mutationType {
              fields {
                name
                description
              }
            }
          }
        }
        """
        
        try:
            response = self.session.post(self.graphql_url, json={'query': schema_query})
            if response.status_code == 200:
                return response.json().get('data', {})
        except Exception as e:
            print(f"Failed to get schema: {e}")
        
        return {}
    
    def print_schema_structure(self, schema: Dict):
        """Print detailed schema structure for debugging"""
        if '__schema' in schema and 'queryType' in schema['__schema']:
            query_fields = schema['__schema']['queryType']['fields']
            
            print("\nAvailable GraphQL Query Fields:")
            for field in query_fields:
                field_name = field['name']
                field_type = field.get('type', {})
                
                if 'page' in field_name.lower():
                    print(f"\n📄 {field_name}:")
                    print(f"  Type: {field_type.get('name', 'Unknown')}")
                    print(f"  Kind: {field_type.get('kind', 'Unknown')}")
                    
                    # Print available fields for this type
                    if 'fields' in field_type and field_type['fields']:
                        print("  Available fields:")
                        for subfield in field_type['fields']:
                            print(f"    - {subfield['name']}: {subfield.get('type', {}).get('name', 'Unknown')}")
                    
                    # Print args if available
                    if field.get('args'):
                        print("  Arguments:")
                        for arg in field['args']:
                            print(f"    - {arg['name']}: {arg.get('type', {}).get('name', 'Unknown')}")

    def find_pages_query_structure(self, schema: Dict) -> List[str]:
        """Build correct queries based on actual schema structure"""
        queries_to_try = []
        
        # Print schema for debugging
        self.print_schema_structure(schema)
        
        # WikiJS specific queries based on common patterns
        queries_to_try.extend([
            # Query 1: Standard WikiJS pages.list structure
            """
            query GetPagesList {
              pages {
                list {
                  id
                  path
                  title
                  isPublished
                  locale
                  createdAt
                  updatedAt
                }
              }
            }
            """,
            
            # Query 2: Simple pages list without content
            """
            query GetPagesSimple {
              pages {
                list {
                  id
                  path
                  title
                  isPublished
                }
              }
            }
            """,
            
            # Query 3: Try pages with search (common WikiJS pattern)
            """
            query SearchAllPages {
              pages {
                search(query: "", path: "/") {
                  results {
                    id
                    path
                    title
                    description
                  }
                }
              }
            }
            """,
            
            # Query 4: Individual page query to understand structure
            """
            query GetSinglePage {
              pages {
                single(id: 1) {
                  id
                  path
                  title
                  content
                  contentType
                  isPublished
                  locale
                  createdAt
                  updatedAt
                }
              }
            }
            """,
            
            # Query 5: Try alternative structure
            """
            {
              pages {
                list {
                  id
                  path
                  title
                  description
                  isPublished
                  locale
                  createdAt
                  updatedAt
                }
              }
            }
            """,
            
            # Query 6: Minimal working query
            """
            query MinimalPages {
              pages {
                list {
                  id
                  path
                  title
                }
              }
            }
            """,
            
            # Query 7: Try tree structure (WikiJS sometimes uses this)
            """
            query GetPagesTree {
              pages {
                tree {
                  id
                  path
                  title
                  isPublished
                }
              }
            }
            """
        ])
        
        return queries_to_try
    
    def fetch_all_pages(self) -> List[Dict]:
        """Fetch all pages using GraphQL with multiple query attempts"""
        print("\nFetching all pages via GraphQL...")
        
        # Get schema information
        schema = self.get_full_schema()
        
        # Generate queries based on schema
        queries = self.find_pages_query_structure(schema)
        
        for i, query in enumerate(queries):
            print(f"\nTrying pages query {i+1}/{len(queries)}...")
            print(f"Query preview: {query.strip()[:100]}...")
            
            try:
                response = self.session.post(self.graphql_url, json={'query': query})
                
                print(f"Response status: {response.status_code}")
                
                if response.status_code == 200:
                    result = response.json()
                    
                    if 'errors' in result:
                        print(f"GraphQL errors: {result['errors']}")
                        continue
                    
                    if 'data' in result:
                        data = result['data']
                        pages = []
                        
                        # Try to extract pages from different response structures
                        if 'pages' in data:
                            pages_data = data['pages']
                            
                            if isinstance(pages_data, list):
                                pages = pages_data
                            elif isinstance(pages_data, dict):
                                if 'list' in pages_data:
                                    pages = pages_data['list']
                                else:
                                    pages = [pages_data]  # Single page object
                        
                        if pages:
                            print(f"✓ Successfully retrieved {len(pages)} pages!")
                            return pages
                        else:
                            print(f"No pages found in response: {list(data.keys())}")
                
                else:
                    print(f"HTTP Error: {response.text[:200]}")
                    
            except Exception as e:
                print(f"Query failed: {e}")
        
        print("✗ All page queries failed")
        return []
    
    def fetch_page_content(self, page_id: str, page_path: str) -> Optional[Dict]:
        """Fetch full content for a specific page"""
        # Multiple query patterns for getting individual page content
        queries = [
            # WikiJS standard single page query
            f"""
            query GetPageContent {{
              pages {{
                single(id: {page_id}) {{
                  id
                  path
                  title
                  description
                  content
                  contentType
                  isPublished
                  locale
                  createdAt
                  updatedAt
                  editor
                }}
              }}
            }}
            """,
            
            # Alternative single page structure
            f"""
            query GetSinglePage {{
              page(id: {page_id}) {{
                id
                path
                title
                description
                content
                contentType
                isPublished
                locale
                createdAt
                updatedAt
                editor
              }}
            }}
            """,
            
            # By path query
            f"""
            query GetPageByPath {{
              pages {{
                single(path: "{page_path}") {{
                  id
                  path
                  title
                  content
                  contentType
                  isPublished
                  locale
                }}
              }}
            }}
            """,
            
            # Simple path query
            f"""
            query GetPageByPath2 {{
              page(path: "{page_path}") {{
                id
                path
                title
                content
                contentType
                isPublished
                locale
              }}
            }}
            """
        ]
        
        for i, query in enumerate(queries, 1):
            try:
                print(f"    Trying content query {i} for page {page_id}")
                response = self.session.post(self.graphql_url, json={'query': query})
                
                if response.status_code == 200:
                    result = response.json()
                    
                    if 'errors' in result:
                        print(f"      GraphQL errors: {result['errors']}")
                        continue
                    
                    if 'data' in result:
                        data = result['data']
                        
                        # Try different response structures
                        page_data = None
                        if 'pages' in data and data['pages'] and 'single' in data['pages']:
                            page_data = data['pages']['single']
                        elif 'page' in data and data['page']:
                            page_data = data['page']
                        
                        if page_data:
                            print(f"      ✓ Got content for page {page_id}")
                            return page_data
                        else:
                            print(f"      No page data in response: {list(data.keys())}")
                else:
                    print(f"      HTTP {response.status_code}: {response.text[:100]}")
                        
            except Exception as e:
                print(f"      Exception: {e}")
        
        return None
    
    def fetch_assets_list(self) -> List[Dict]:
        """Fetch list of all assets/attachments"""
        print("\nFetching assets list...")
        
        # Start by getting all folders to discover all assets
        print("\nStep 1: Getting folder structure...")
        folders = self.get_all_folders()
        
        print(f"\nStep 2: Getting assets from all {len(folders)} folders...")
        return self.get_all_assets_from_folders(folders)
    
    def get_all_folders(self) -> List[Dict]:
        """Get all folders recursively and build folder path mapping"""
        all_folders = [{'id': 0, 'name': 'root', 'path': ''}]  # Start with root folder
        folders_to_check = [0]  # Start with root
        folder_paths = {0: ''}  # Map folder ID to full path
        
        while folders_to_check:
            parent_id = folders_to_check.pop(0)
            parent_path = folder_paths.get(parent_id, '')
            
            try:
                query = f"""
                query GetSubfolders {{
                  assets {{
                    folders(parentFolderId: {parent_id}) {{
                      id
                      name
                      slug
                    }}
                  }}
                }}
                """
                
                response = self.session.post(self.graphql_url, json={'query': query})
                
                if response.status_code == 200:
                    result = response.json()
                    
                    if 'data' in result and 'assets' in result['data'] and 'folders' in result['data']['assets']:
                        subfolders = result['data']['assets']['folders']
                        
                        for folder in subfolders:
                            folder_id = folder.get('id')
                            folder_name = folder.get('name', 'Unnamed')
                            
                            if folder_id and folder_id not in [f['id'] for f in all_folders]:
                                # Build full path for this folder
                                if parent_path:
                                    full_path = f"{parent_path}/{folder_name}"
                                else:
                                    full_path = folder_name
                                
                                folder['path'] = full_path
                                folder_paths[folder_id] = full_path
                                
                                all_folders.append(folder)
                                folders_to_check.append(folder_id)  # Check this folder for subfolders
                                print(f"  Found folder: {full_path} (ID: {folder_id})")
                                
            except Exception as e:
                print(f"  Failed to get subfolders of {parent_id}: {e}")
        
        print(f"  Total folders found: {len(all_folders)}")
        
        # Store folder paths for later use
        self.folder_paths = folder_paths
        return all_folders

    def discover_asset_types(self, sample_folder: Dict) -> List[Dict]:
        """Discover what asset types are available by sampling a folder"""
        folder_id = sample_folder.get('id', 0)

        try:
            # Try to get a sample of assets without kind filter
            query = f"""
            query DiscoverAssetTypes {{
              assets {{
                list(folderId: {folder_id}) {{
                  kind
                  filename
                  ext
                  mime
                }}
              }}
            }}
            """

            response = self.session.post(self.graphql_url, json={'query': query})

            if response.status_code == 200:
                result = response.json()

                if ('data' in result and 'assets' in result['data']
                    and 'list' in result['data']['assets']):
                    return result['data']['assets']['list']

        except Exception as e:
            print(f"    Failed to discover asset types: {e}")

        return []

    def get_all_assets_from_folders(self, folders: List[Dict]) -> List[Dict]:
        """Get all assets from all folders (all types including XML, documents, etc.)"""
        all_assets = []

        # First, try to get all assets without kind filter to discover all types
        print("  Discovering available asset types...")
        sample_assets = self.discover_asset_types(folders[0] if folders else {'id': 0})

        # Extract unique asset kinds from sample
        discovered_kinds = set()
        for asset in sample_assets:
            kind = asset.get('kind')
            if kind:
                discovered_kinds.add(kind)

        if not discovered_kinds:
            # Fallback to common asset types if discovery fails
            discovered_kinds = {'IMAGE', 'BINARY', 'DOCUMENT', 'VIDEO', 'AUDIO', 'OTHER'}

        print(f"  Asset types to fetch: {sorted(discovered_kinds)}")

        for folder in folders:
            folder_id = folder.get('id')
            folder_path = folder.get('path', '')  # Use full folder path

            print(f"  Checking folder: {folder_path if folder_path else 'root'} (ID: {folder_id})")

            # Try to get all assets without kind filter first
            try:
                query = f"""
                query GetAllAssetsFromFolder {{
                  assets {{
                    list(folderId: {folder_id}) {{
                      id
                      filename
                      ext
                      kind
                      mime
                      fileSize
                      folder {{
                        name
                      }}
                    }}
                  }}
                }}
                """

                response = self.session.post(self.graphql_url, json={'query': query})

                if response.status_code == 200:
                    result = response.json()

                    if ('data' in result and 'assets' in result['data']
                        and 'list' in result['data']['assets']):
                        folder_assets = result['data']['assets']['list']

                        if folder_assets:
                            print(f"    Found {len(folder_assets)} assets (all types)")
                            # Add full folder path to each asset
                            for asset in folder_assets:
                                asset['folder_path'] = folder_path  # Add full path
                            all_assets.extend(folder_assets)
                            continue  # Skip individual kind queries if this worked

            except Exception as e:
                print(f"    Failed to get all assets from folder {folder_id}: {e}")

            # Fallback: Get assets by individual kinds
            for kind in discovered_kinds:
                try:
                    query = f"""
                    query GetAssetsFromFolder {{
                      assets {{
                        list(folderId: {folder_id}, kind: {kind}) {{
                          id
                          filename
                          ext
                          kind
                          mime
                          fileSize
                          folder {{
                            name
                          }}
                        }}
                      }}
                    }}
                    """

                    response = self.session.post(self.graphql_url, json={'query': query})

                    if response.status_code == 200:
                        result = response.json()

                        if ('data' in result and 'assets' in result['data']
                            and 'list' in result['data']['assets']):
                            folder_assets = result['data']['assets']['list']

                            if folder_assets:
                                print(f"    Found {len(folder_assets)} {kind} assets")
                                # Add full folder path to each asset
                                for asset in folder_assets:
                                    asset['folder_path'] = folder_path  # Add full path
                                all_assets.extend(folder_assets)

                except Exception as e:
                    print(f"    Failed to get {kind} assets from folder {folder_id}: {e}")

        # Remove duplicates based on filename and folder path
        unique_assets = {}
        for asset in all_assets:
            filename = asset.get('filename')
            folder_path = asset.get('folder_path', '')
            asset_key = f"{folder_path}/{filename}" if folder_path else filename

            if filename and asset_key not in unique_assets:
                unique_assets[asset_key] = asset

        final_assets = list(unique_assets.values())
        print(f"\n  ✓ Total unique assets found: {len(final_assets)}")

        # Print asset type breakdown
        type_counts = {}
        for asset in final_assets:
            asset_type = asset.get('kind', 'UNKNOWN')
            type_counts[asset_type] = type_counts.get(asset_type, 0) + 1

        print("  Asset type breakdown:")
        for asset_type, count in sorted(type_counts.items()):
            print(f"    {asset_type}: {count}")

        return final_assets
    
    
    def download_asset(self, filename: str, folder: str = "") -> bool:
        """Download an asset file (only from this WikiJS instance)"""
        # Clean filename - remove size parameters like =500x, =300x200, =40%x, =70%x, etc.
        import re
        import urllib.parse

        # Security check: Don't download if filename contains suspicious patterns
        if any(pattern in filename.lower() for pattern in ['http://', 'https://', '../', '..']):
            print(f"  Skipping suspicious filename: {filename}")

            # Track as failed download
            asset_key = filename.lstrip('/')
            if asset_key not in self.failed_downloads:
                self.failed_downloads[asset_key] = []
            self.failed_downloads[asset_key].append("Suspicious filename (security)")

            return False

        # First decode URL encoding
        decoded_filename = urllib.parse.unquote(filename)

        # Remove various size parameter patterns:
        # =500x300, =500x, =40%x, =70%x, etc.
        size_patterns = [
            r'\s*=\d+%?x\d*%?\s*$',  # =40%x, =70%x, =500x300, =500x
            r'\s*=\d+%?\s*$',        # =40%, =500
            r'\s*=\d+x\d+\s*$',      # =500x300
            r'\s*=\d+x\s*$',         # =500x
        ]

        clean_filename = decoded_filename
        for pattern in size_patterns:
            clean_filename = re.sub(pattern, '', clean_filename)

        print(f"  Original: {filename}")
        if clean_filename != decoded_filename:
            print(f"  Cleaned:  {clean_filename}")
        
        # Construct asset URL
        if folder:
            asset_path = f"/{folder}/{clean_filename}"
        else:
            asset_path = f"/{clean_filename}"
        
        # Check if already downloaded
        asset_key = asset_path.lstrip('/')
        if asset_key in self.successfully_downloaded:
            print(f"  Skipping {asset_path} (already downloaded)")
            return True
        
        asset_url = f"{self.wiki_url}{asset_path}"
        
        try:
            print(f"  Downloading: {asset_path}")

            # Try different asset URL patterns (all relative to this wiki instance)
            # This ensures we only download files hosted on this WikiJS instance
            asset_urls_to_try = [
                asset_url,  # Direct path - works!
                f"{self.wiki_url}/a{asset_path}",  # WikiJS asset page (returns HTML)
                f"{self.wiki_url}/assets{asset_path}",
                f"{self.wiki_url}/uploads{asset_path}",
                f"{self.wiki_url}/files{asset_path}",
                f"{self.wiki_url}/content{asset_path}",
                f"{self.wiki_url}/media{asset_path}",
                f"{self.wiki_url}/static{asset_path}"
            ]
            
            for i, url in enumerate(asset_urls_to_try, 1):
                try:
                    print(f"    Trying {i}: {url}")
                    
                    # Include authentication headers for asset download
                    headers = {
                        'Authorization': f'Bearer {self.api_token}',
                        'User-Agent': 'WikiJS-Exporter/1.0'
                    }
                    
                    response = requests.get(url, headers=headers, stream=True, timeout=10)
                    print(f"      Status: {response.status_code}")
                    
                    if response.status_code == 200:
                        # Verify it's not an HTML error page
                        content_type = response.headers.get('content-type', '')
                        if 'text/html' in content_type.lower():
                            print(f"      Skipping HTML response")
                            continue
                            
                        # Create local path
                        local_path = self.output_dir / asset_path.lstrip('/')
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        
                        # Write file
                        with open(local_path, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=8192):
                                f.write(chunk)
                        
                        file_size = local_path.stat().st_size
                        print(f"    ✓ Downloaded: {local_path} ({file_size:,} bytes) - skipping remaining URLs")
                        
                        # Mark as successfully downloaded
                        self.successfully_downloaded.add(asset_key)
                        return True
                        
                except Exception as e:
                    print(f"      Error: {e}")
                    continue
            
            print(f"    ✗ Failed to download {filename}")

            # Track failed download
            asset_key = asset_path.lstrip('/')
            if asset_key not in self.failed_downloads:
                self.failed_downloads[asset_key] = []
            self.failed_downloads[asset_key].append("All download URLs failed")

            return False
            
        except Exception as e:
            print(f"    ✗ Error downloading {filename}: {e}")
            return False
    
    def save_page_as_markdown(self, page: Dict) -> Path:
        """Save page as markdown file with frontmatter (same format as git export)"""
        # Extract page information
        page_path = page.get('path', '').strip('/')
        title = page.get('title', 'Untitled')
        content = page.get('content', '')
        
        # Determine file path (same structure as git export)
        if page_path:
            if page_path == 'home':
                file_path = self.output_dir / "home.md"
            else:
                file_path = self.output_dir / f"{page_path}.md"
        else:
            file_path = self.output_dir / "index.md"
        
        # Create directory structure
        file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Build frontmatter (same format as WikiJS git export)
        frontmatter = {
            'title': title,
            'description': page.get('description', ''),
            'published': page.get('isPublished', True),
            'date': page.get('updatedAt', ''),
            'tags': [tag.get('tag', '') for tag in page.get('tags', []) if tag.get('tag')],
            'editor': page.get('editor', 'markdown'),
            'dateCreated': page.get('createdAt', '')
        }
        
        # Clean empty values
        frontmatter = {k: v for k, v in frontmatter.items() if v or k == 'published'}
        
        # Write markdown file with YAML frontmatter
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write('---\n')
            for key, value in frontmatter.items():
                if isinstance(value, list) and value:
                    f.write(f'{key}: {json.dumps(value)}\n')
                elif not isinstance(value, list):
                    f.write(f'{key}: {value}\n')
            f.write('---\n\n')
            f.write(content)
        
        print(f"  ✓ Saved: {file_path}")
        
        # Extract and queue assets for download
        self.extract_and_queue_assets(content, file_path)
        
        return file_path
    
    def extract_and_queue_assets(self, content: str, page_path: Path):
        """Extract asset references from content and queue them for download"""

        # Enhanced patterns to capture more file types and formats
        asset_patterns = [
            r'!\[([^\]]*)\]\(([^)]+)\)',  # Markdown images
            r'<img[^>]+src=["\']([^"\']+)["\']',  # HTML img tags
            # Enhanced markdown link patterns for files
            r'\[([^\]]*)\]\((/[^)]+\.(?:xml|txt|csv|json|pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|7z|yaml|yml|drawio)(?:\s*=[^)]*)?)\)',
            r'\[([^\]]*)\]\(([^)]+\.(?:xml|txt|csv|json|pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|7z|yaml|yml|drawio)(?:\s*=[^)]*)?)\)',  # Any file with these extensions
            # HTML links to files
            r'<a[^>]+href=["\']([^"\']+\.(?:xml|txt|csv|json|pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|7z|yaml|yml|drawio)(?:\s*=[^"\']*)?)["\']',
            # Additional patterns for common file extensions in various contexts
            r'src=["\']([^"\']*\.(?:jpg|jpeg|png|gif|svg|webp|pdf|doc|docx|xls|xlsx|ppt|pptx|xml|txt|zip|rar|7z|yaml|yml)(?:\s*=[^"\']*)?)["\']',
            r'href=["\']([^"\']*\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx|xml|txt|zip|rar|7z|csv|json|yaml|yml|drawio)(?:\s*=[^"\']*)?)["\']',
        ]

        print(f"    Extracting file references from {page_path.name}")
        
        for pattern in asset_patterns:
            matches = re.findall(pattern, content)
            for match in matches:
                if isinstance(match, tuple):
                    asset_url = match[-1]  # Get the URL part
                else:
                    asset_url = match

                if asset_url and self.is_wiki_hosted_asset(asset_url):
                    # Clean the asset URL
                    cleaned_url = self.clean_asset_url(asset_url)
                    if cleaned_url:
                        asset_key = cleaned_url.lstrip('/')
                        self.downloaded_assets.add(asset_key)
                        print(f"      Found file reference: {asset_key}")

                        # Track which page references this asset
                        page_name = page_path.name
                        if asset_key not in self.asset_to_pages:
                            self.asset_to_pages[asset_key] = []
                        if page_name not in self.asset_to_pages[asset_key]:
                            self.asset_to_pages[asset_key].append(page_name)

    def clean_asset_url(self, asset_url: str) -> str:
        """Clean asset URL by removing size parameters and decoding"""
        import urllib.parse
        import re

        # First decode URL encoding
        decoded_url = urllib.parse.unquote(asset_url)

        # Remove various size parameter patterns from the URL
        size_patterns = [
            r'\s*=\d+%?x\d*%?\s*$',  # =40%x, =70%x, =500x300, =500x
            r'\s*=\d+%?\s*$',        # =40%, =500
            r'\s*=\d+x\d+\s*$',      # =500x300
            r'\s*=\d+x\s*$',         # =500x
        ]

        clean_url = decoded_url
        for pattern in size_patterns:
            clean_url = re.sub(pattern, '', clean_url)

        return clean_url

    def is_wiki_hosted_asset(self, asset_url: str) -> bool:
        """Check if the asset URL is hosted on this WikiJS instance"""
        import urllib.parse

        # If it's a relative URL (no protocol), it's likely hosted on the wiki
        if not asset_url.startswith(('http://', 'https://', '//')):
            return True

        # If it's an absolute URL, check if it's from the same domain
        try:
            parsed_asset = urllib.parse.urlparse(asset_url)
            parsed_wiki = urllib.parse.urlparse(self.wiki_url)

            # Same domain/host means it's hosted on this wiki
            if parsed_asset.netloc.lower() == parsed_wiki.netloc.lower():
                return True

            # Also check if it's a subdomain of the wiki domain
            wiki_domain = parsed_wiki.netloc.lower()
            asset_domain = parsed_asset.netloc.lower()

            # Allow subdomains (e.g., cdn.wiki.example.com for wiki.example.com)
            if asset_domain.endswith('.' + wiki_domain):
                return True

        except Exception as e:
            print(f"    Warning: Could not parse URL {asset_url}: {e}")
            return False

        # External URL - don't download
        print(f"    Skipping external asset: {asset_url}")
        return False

    def generate_failed_assets_log(self):
        """Generate a log file mapping MD files to failed asset downloads"""
        if not self.failed_downloads:
            print("✓ No failed asset downloads to log")
            return

        log_file = self.output_dir / '_failed_assets_log.md'

        with open(log_file, 'w', encoding='utf-8') as f:
            f.write("# Failed Asset Downloads Report\n\n")
            f.write(f"Generated: {datetime.now().isoformat()}\n")
            f.write(f"Wiki URL: {self.wiki_url}\n\n")

            f.write(f"## Summary\n\n")
            f.write(f"- **Total failed assets**: {len(self.failed_downloads)}\n")
            f.write(f"- **Total exported pages**: {len(self.exported_pages)}\n")
            f.write(f"- **Successfully downloaded assets**: {len(self.successfully_downloaded)}\n\n")

            f.write("## Failed Assets by Page\n\n")

            # Group failed assets by the pages that reference them
            page_to_failed_assets = {}
            orphaned_assets = []

            for asset_path, reasons in self.failed_downloads.items():
                referencing_pages = self.asset_to_pages.get(asset_path, [])

                if referencing_pages:
                    for page in referencing_pages:
                        if page not in page_to_failed_assets:
                            page_to_failed_assets[page] = []
                        page_to_failed_assets[page].append((asset_path, reasons))
                else:
                    orphaned_assets.append((asset_path, reasons))

            # Write failed assets grouped by page
            for page_name in sorted(page_to_failed_assets.keys()):
                f.write(f"### 📄 {page_name}\n\n")

                failed_assets = page_to_failed_assets[page_name]
                for asset_path, reasons in failed_assets:
                    f.write(f"- **{asset_path}**\n")
                    for reason in reasons:
                        f.write(f"  - ❌ {reason}\n")
                f.write("\n")

            # Write orphaned failed assets (not referenced by any exported page)
            if orphaned_assets:
                f.write("### 🔍 Assets Not Referenced by Exported Pages\n\n")
                f.write("These assets failed to download but were not found in any exported page content:\n\n")

                for asset_path, reasons in orphaned_assets:
                    f.write(f"- **{asset_path}**\n")
                    for reason in reasons:
                        f.write(f"  - ❌ {reason}\n")
                f.write("\n")

            f.write("## All Failed Assets (Alphabetical)\n\n")
            for asset_path in sorted(self.failed_downloads.keys()):
                reasons = self.failed_downloads[asset_path]
                referencing_pages = self.asset_to_pages.get(asset_path, [])

                f.write(f"### {asset_path}\n\n")
                f.write("**Failure reasons:**\n")
                for reason in reasons:
                    f.write(f"- ❌ {reason}\n")

                if referencing_pages:
                    f.write("\n**Referenced by pages:**\n")
                    for page in referencing_pages:
                        f.write(f"- 📄 {page}\n")
                else:
                    f.write("\n**Referenced by:** *(No exported pages)*\n")
                f.write("\n")

        print(f"✓ Failed assets log saved: {log_file}")
        print(f"  - {len(self.failed_downloads)} failed assets logged")

        # Also create a simple CSV for easy processing
        csv_file = self.output_dir / '_failed_assets.csv'
        with open(csv_file, 'w', encoding='utf-8') as f:
            f.write("Asset Path,Failure Reason,Referencing Pages\n")
            for asset_path, reasons in self.failed_downloads.items():
                referencing_pages = self.asset_to_pages.get(asset_path, [])
                reason_str = "; ".join(reasons)
                pages_str = "; ".join(referencing_pages) if referencing_pages else "None"
                f.write(f'"{asset_path}","{reason_str}","{pages_str}"\n')

        print(f"✓ Failed assets CSV saved: {csv_file}")

    def export_complete_wiki(self):
        """Main export function - exports everything"""
        print("=" * 60)
        print("WikiJS Complete GraphQL Export")
        print("=" * 60)
        
        # Step 1: Test connection
        if not self.test_graphql_connection():
            print("\n✗ Cannot establish GraphQL connection")
            print("\nTroubleshooting:")
            print("1. Check if GraphQL API is enabled in WikiJS admin panel")
            print("2. Verify API token permissions")
            print("3. Check if WikiJS has API rate limiting enabled")
            print("4. Try using admin account token")
            return
        
        if not self.assets_only:
            # Step 2: Fetch all pages
            pages = self.fetch_all_pages()
            if not pages:
                print("\n✗ Could not fetch any pages")
                return
            
            print(f"\n📄 Found {len(pages)} pages to export")
            
            # Step 3: Export pages with full content
            print("\n" + "="*40)
            print("EXPORTING PAGES")
            print("="*40)
            
            for i, page in enumerate(pages, 1):
                page_id = page.get('id')
                page_path = page.get('path', f'page_{page_id}')
                page_title = page.get('title', 'Untitled')
                
                print(f"[{i:3d}/{len(pages)}] {page_title} ({page_path})")
                
                try:
                    # Get full content if not already included
                    if not page.get('content') and page_id:
                        full_page = self.fetch_page_content(page_id, page_path)
                        if full_page:
                            page.update(full_page)
                    
                    # Save page
                    self.save_page_as_markdown(page)
                    self.exported_pages.append(page)
                    
                    # Rate limiting
                    time.sleep(0.2)
                    
                except Exception as e:
                    print(f"  ✗ Failed: {e}")
        else:
            print("\n🎯 Assets-only mode: Skipping page export")
        
        # Step 4: Export assets
        print("\n" + "="*40)
        print("EXPORTING ASSETS")
        print("="*40)
        
        # Try to get assets via GraphQL
        assets = self.fetch_assets_list()
        
        # Download assets found via GraphQL
        for asset in assets:
            filename = asset.get('filename', '')
            
            # Use full folder path instead of just folder name
            folder_path = asset.get('folder_path', '')
            
            if filename:
                self.download_asset(filename, folder_path)
        
        # Download assets referenced in content
        print(f"\nDownloading {len(self.downloaded_assets)} assets referenced in content...")
        for asset_path in self.downloaded_assets:
            if '/' in asset_path:
                folder, filename = asset_path.rsplit('/', 1)
            else:
                folder, filename = '', asset_path
            
            self.download_asset(filename, folder)

        # Step 5: Export summary
        print("\n" + "="*60)
        print("EXPORT COMPLETED")
        print("="*60)
        print(f"✓ Pages exported: {len(self.exported_pages)}")
        print(f"✓ Assets downloaded: {len(self.successfully_downloaded)}")
        print(f"✓ Output directory: {self.output_dir}")
        
        # Create export manifest
        manifest = {
            'export_timestamp': datetime.now().isoformat(),
            'wiki_url': self.wiki_url,
            'pages_count': len(self.exported_pages),
            'assets_count': len(self.successfully_downloaded),
            'pages': [
                {
                    'title': page.get('title'),
                    'path': page.get('path'),
                    'id': page.get('id')
                }
                for page in self.exported_pages
            ]
        }
        
        with open(self.output_dir / '_export_manifest.json', 'w') as f:
            json.dump(manifest, f, indent=2)

        print(f"✓ Export manifest saved: {self.output_dir / '_export_manifest.json'}")

        # Generate failed assets log
        self.generate_failed_assets_log()

def main():
    parser = argparse.ArgumentParser(description='Complete WikiJS GraphQL Export')
    parser.add_argument('--wiki-url', required=True, help='WikiJS instance URL (e.g., https://wiki.bringauto.com)')
    parser.add_argument('--token', required=True, help='WikiJS API token (generate in admin panel)')
    parser.add_argument('--output-dir', default='./wikijs-complete-export', help='Output directory for export')
    parser.add_argument('--assets-only', action='store_true', help='Download only assets (skip pages)')
    
    args = parser.parse_args()
    
    exporter = WikiJSGraphQLExporter(args.wiki_url, args.token, args.output_dir, args.assets_only)
    exporter.export_complete_wiki()

if __name__ == '__main__':
    main()