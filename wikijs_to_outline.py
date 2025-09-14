#!/usr/bin/env python3
"""
WikiJS to Outline Migration Script

This script migrates a WikiJS backup (git repository format) to Outline wiki,
preserving page hierarchy, images, and updating crosslinks.

Usage:
    python wikijs_to_outline.py --outline-url https://your-outline.com --token YOUR_API_TOKEN --wiki-dir ./wiki.js-backup
"""

import re
import argparse
import requests
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import time
import mimetypes
import os
from datetime import datetime

class WikiJSToOutlineConverter:
    def __init__(self, outline_url: str, api_token: str, wiki_dir: str):
        self.outline_url = outline_url.rstrip('/')
        self.api_token = api_token
        self.wiki_dir = Path(wiki_dir)
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json'
        })
        
        # Maps to track created documents and their new URLs
        self.document_map: Dict[str, str] = {}  # old_path -> new_document_id
        self.url_map: Dict[str, str] = {}  # old_url -> new_url
        self.collection_id: Optional[str] = None

        # File-level log: md_relative_path -> events
        self.file_log: Dict[str, Dict[str, List[Dict]]] = {}

        def _init_file_log(rel: str):
            return {
                'document': [],      # create/update events
                'attachments': [],   # uploads per image/file
                'move': [],          # hierarchy moves
                'crosslinks': []     # crosslink updates
            }

        self._init_file_log = _init_file_log
    
    def _resolve_file_path(self, file_path: str, base_path: Path) -> Path:
        """Resolve relative or absolute file paths consistently"""
        if file_path.startswith('/'):
            return self.wiki_dir / file_path.lstrip('/')
        else:
            return base_path.parent / file_path
        
    def get_collections(self) -> List[Dict]:
        """Get all collections from Outline"""
        try:
            response = self.session.post(f'{self.outline_url}/api/collections.list')
            if response.status_code == 401:
                print(f"Authentication failed. Please check your API token.")
                print(f"URL: {self.outline_url}/api/collections.list")
                print(f"Token starts with: {self.api_token[:10]}...")
                raise Exception("Invalid API token or insufficient permissions")
            response.raise_for_status()
            return response.json()['data']
        except requests.exceptions.RequestException as e:
            print(f"API request failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"Response status: {e.response.status_code}")
                print(f"Response text: {e.response.text}")
            raise
    
    def create_collection(self, name: str, description: str = "") -> str:
        """Create a new collection and return its ID"""
        data = {
            'name': name,
            'description': description
        }
        response = self.session.post(f'{self.outline_url}/api/collections.create', json=data)
        response.raise_for_status()
        return response.json()['data']['id']
    
    def _log_event(self, rel_path: str, category: str, status: str, message: str, extra: Optional[Dict] = None):
        if rel_path not in self.file_log:
            self.file_log[rel_path] = self._init_file_log(rel_path)
        entry = {'status': status, 'message': message}
        if extra:
            entry.update(extra)
        self.file_log[rel_path][category].append(entry)

    def _handle_upload_error(self, md_rel_path: Optional[str], message: str, file_path: Path, extra: Optional[Dict] = None):
        """Helper method to handle upload errors consistently"""
        print(f"  {message}")
        if md_rel_path:
            log_extra = {'file': str(file_path)}
            if extra:
                log_extra.update(extra)
            self._log_event(md_rel_path, 'attachments', 'failed', message, log_extra)

    def _cleanup_temp_file(self, temp_path: Path):
        """Safely clean up temporary files"""
        try:
            temp_path.unlink()
        except Exception:
            pass

    def upload_attachment(self, file_path: Path, md_rel_path: Optional[str] = None) -> str:
        """Upload an image/attachment to Outline using attachments API"""
        if not file_path.exists():
            if md_rel_path:
                self._log_event(md_rel_path, 'attachments', 'failed', 'File not found', {'file': str(file_path)})
            raise FileNotFoundError(f"File not found: {file_path}")
            
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if not mime_type:
            mime_type = 'application/octet-stream'
            
        # Get file size
        file_size = file_path.stat().st_size
        
        # Check Outline's 1MB limit - compress if needed
        max_outline_size = 1000000 * 10
        if file_size > max_outline_size:
            print(f"  File too large ({file_size:,} bytes), compressing for attachment upload")
            compressed_path = self.compress_image(file_path, max_outline_size)
            if compressed_path and compressed_path.stat().st_size <= max_outline_size:
                print(f"  ✓ Compressed: {file_size:,} → {compressed_path.stat().st_size:,} bytes")
                try:
                    result = self.upload_attachment(compressed_path, md_rel_path)
                    self._cleanup_temp_file(compressed_path)
                    return result
                except Exception:
                    self._cleanup_temp_file(compressed_path)
                    raise
            # If compression did not succeed, continue with original file upload

        # Upload via attachments API
        print(f"  Uploading attachment: {file_path.name} ({file_size:,} bytes)")
        
        try:
            # Step 1: Create attachment entry
            create_data = {
                'name': file_path.name,
                'contentType': mime_type,
                'size': file_size
            }
            
            response = self.session.post(f'{self.outline_url}/api/attachments.create', json=create_data)

            if response.status_code != 200:
                self._handle_upload_error(md_rel_path, f"Failed to create attachment: {response.status_code} - {response.text}", 
                                        file_path, {'url': f'{self.outline_url}/api/attachments.create'})
                raise Exception(f"attachments.create failed: {response.status_code}")
            
            data = response.json()['data']
            upload_url = data.get('uploadUrl')
            form_data = data.get('form', {})
            attachment = data.get('attachment', {})
            
            if not upload_url:
                self._handle_upload_error(md_rel_path, "No upload URL received", file_path)
                raise Exception("No upload URL received")
            
            # Step 2: Upload file to storage
            if upload_url.startswith('/'):
                upload_url = f"{self.outline_url.rstrip('/')}{upload_url}"
            
            print(f"  Uploading to: {upload_url}")
            
            with open(file_path, 'rb') as f:
                files = {'file': (file_path.name, f, mime_type)}
                
                # For Outline's local storage, include authorization header
                upload_headers = {'Authorization': f'Bearer {self.api_token}'}
                
                # Try multipart form upload
                upload_response = requests.post(upload_url, data=form_data, files=files, headers=upload_headers)
                
                if upload_response.status_code in [200, 201, 204]:
                    # Upload successful, return attachment URL
                    if 'url' in attachment:
                        attachment_url = attachment['url']
                        if attachment_url.startswith('/'):
                            attachment_url = f"{self.outline_url.rstrip('/')}{attachment_url}"
                        print(f"  ✓ Attachment uploaded: {attachment['name']}")
                        if md_rel_path:
                            self._log_event(md_rel_path, 'attachments', 'success', 'Uploaded attachment', {
                                'file': str(file_path), 'url': attachment_url
                            })
                        return attachment_url
                    else:
                        self._handle_upload_error(md_rel_path, "No attachment URL in response", file_path)
                        raise Exception("No attachment URL returned")
                else:
                    self._handle_upload_error(md_rel_path, f"Upload failed: {upload_response.status_code} - {upload_response.text[:200]}", 
                                            file_path, {'url': upload_url})
                    raise Exception(f"Upload failed: {upload_response.status_code}")

        except Exception as e:
            self._handle_upload_error(md_rel_path, f"Attachment upload failed: {e}", file_path)
            raise
    
    def handle_large_image(self, file_path: Path, max_size: int) -> str:
        """Handle large images by compressing them"""
        try:
            # Try to compress the image first
            compressed_path = self.compress_image(file_path, max_size)
            if compressed_path and compressed_path.stat().st_size <= max_size:
                print(f"  ✓ Compressed {file_path.name}: {file_path.stat().st_size:,} → {compressed_path.stat().st_size:,} bytes")
                # Use base64 embedding for the compressed version
                result = self.upload_attachment_base64_fallback(compressed_path)
                # Clean up temporary file
                self._cleanup_temp_file(compressed_path)
                return result
            
            # If compression didn't work enough, provide placeholder
            file_size_mb = file_path.stat().st_size / (1024 * 1024)
            return f"*Large Image: {file_path.name} ({file_size_mb:.1f}MB - too large to embed)*"
            
        except Exception as e:
            print(f"  Large image handling failed: {e}")
            return f"*Image: {file_path.name} (processing failed)*"
    
    def _create_temp_image_file(self, suffix: str = '.jpg') -> Tuple[int, Path]:
        """Create a temporary image file and return file descriptor and path"""
        import tempfile
        temp_fd, temp_name = tempfile.mkstemp(suffix=suffix)
        return temp_fd, Path(temp_name)

    def _save_and_check_size(self, img, temp_path: Path, temp_fd: int, target_size: int, quality: int) -> bool:
        """Save image and check if it meets size requirements"""
        try:
            img.save(temp_path, 'JPEG', quality=quality, optimize=True)
            return temp_path.stat().st_size <= target_size
        finally:
            os.close(temp_fd)

    def compress_image(self, file_path: Path, target_size: int) -> Optional[Path]:
        """Compress image to fit within target size"""
        try:
            from PIL import Image
            
            # Only compress actual image formats
            if not file_path.suffix.lower() in ['.jpg', '.jpeg', '.png', '.webp']:
                return None
            
            with Image.open(file_path) as img:
                # Convert to RGB if necessary
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                
                # Start with 85% quality for JPEG
                quality = 85
                
                while quality > 20:  # Don't go below 20% quality
                    # Create temporary file
                    temp_fd, temp_path = self._create_temp_image_file()
                    
                    if self._save_and_check_size(img, temp_path, temp_fd, target_size, quality):
                        return temp_path
                    
                    # Too big, try lower quality
                    quality -= 15
                    self._cleanup_temp_file(temp_path)
                
                # If we get here, even lowest quality is too big, try resizing
                return self.resize_and_compress_image(file_path, target_size)
                
        except ImportError:
            print(f"  PIL not available for compression. Install with: pip install Pillow")
            return None
        except Exception as e:
            print(f"  Compression failed: {e}")
            return None
    
    def resize_and_compress_image(self, file_path: Path, target_size: int) -> Optional[Path]:
        """Resize and compress image more aggressively"""
        try:
            from PIL import Image
            
            with Image.open(file_path) as img:
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                
                # Start with 50% of original size
                scale_factor = 0.5
                quality = 75
                
                while scale_factor > 0.1:  # Don't resize smaller than 10%
                    # Resize image
                    new_size = (int(img.width * scale_factor), int(img.height * scale_factor))
                    resized = img.resize(new_size, Image.Resampling.LANCZOS)
                    
                    # Try to compress to target size
                    temp_fd, temp_path = self._create_temp_image_file()
                    
                    if self._save_and_check_size(resized, temp_path, temp_fd, target_size, quality):
                        print(f"    Resized to {new_size[0]}x{new_size[1]} at {quality}% quality")
                        return temp_path
                    
                    # Still too big, try smaller
                    scale_factor -= 0.1
                    quality = max(30, quality - 10)  # Don't go below 30% quality
                    self._cleanup_temp_file(temp_path)
                
            return None
            
        except Exception as e:
            print(f"  Resize and compress failed: {e}")
            return None
    
    def upload_attachment_base64_fallback(self, file_path: Path) -> str:
        """Fallback: Embed image as base64 data URL (per Outline docs)"""
        try:
            import base64
            
            mime_type, _ = mimetypes.guess_type(str(file_path))
            if not mime_type:
                mime_type = 'application/octet-stream'
            
            with open(file_path, 'rb') as f:
                file_content = f.read()
            
            file_b64 = base64.b64encode(file_content).decode('utf-8')
            data_url = f"data:{mime_type};base64,{file_b64}"
            
            print(f"  Using base64 fallback for {file_path.name}")
            return data_url
            
        except Exception as e:
            print(f"  Base64 fallback failed: {e}")
            return str(file_path)
    
    def create_document(self, title: str, content: str) -> Dict:
        """Create a document in Outline as published"""
        data = {
            'title': title,
            'text': content,
            'collectionId': self.collection_id,
            'publish': True  # Create as published, no need to publish later
        }

        response = self.session.post(f'{self.outline_url}/api/documents.create', json=data)
        response.raise_for_status()
        return response.json()['data']

    def write_log_files(self):
        """Write per-file migration logs (Markdown + CSV of failures)"""
        if not self.file_log:
            return
        out_dir = self.wiki_dir
        md_path = out_dir / '_outline_migration_log.md'
        csv_path = out_dir / '_outline_migration_failures.csv'

        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(f"# Outline Migration Log\n\n")
            f.write(f"Generated: {datetime.now().isoformat()}\n\n")
            for rel_path in sorted(self.file_log.keys()):
                f.write(f"## {rel_path}\n\n")
                sections = ['document', 'attachments', 'move', 'crosslinks']
                for sec in sections:
                    events = self.file_log[rel_path].get(sec, [])
                    if not events:
                        continue
                    f.write(f"### {sec.title()}\n")
                    for e in events:
                        status = e.get('status', 'info')
                        msg = e.get('message', '')
                        detail = []
                        if 'file' in e:
                            detail.append(f"file={e['file']}")
                        if 'url' in e:
                            detail.append(f"url={e['url']}")
                        if 'id' in e:
                            detail.append(f"id={e['id']}")
                        suffix = f" ({', '.join(detail)})" if detail else ''
                        f.write(f"- {status.upper()}: {msg}{suffix}\n")
                    f.write("\n")

        # CSV of failures only
        with open(csv_path, 'w', encoding='utf-8') as f:
            f.write("file,category,status,message,extra\n")
            for rel_path, sections in self.file_log.items():
                for category, events in sections.items():
                    for e in events:
                        if e.get('status') != 'failed':
                            continue
                        extra_parts = []
                        for k in ('file','url','id'):
                            if k in e:
                                extra_parts.append(f"{k}={e[k]}")
                        extra = '; '.join(extra_parts)
                        # Escape commas in message
                        msg = e.get('message','').replace(',', ';')
                        f.write(f"{rel_path},{category},{e.get('status')},{msg},{extra}\n")
    
    def move_document(self, document_id: str, parent_id: Optional[str] = None) -> bool:
        """Move a document to a new parent using documents.move API"""
        data = {
            'id': document_id,
            'collectionId': self.collection_id
        }
        
        if parent_id:
            data['parentDocumentId'] = parent_id
        
        response = self.session.post(f'{self.outline_url}/api/documents.move', json=data)
        
        if response.status_code != 200:
            print(f"  Failed to move document: {response.status_code} - {response.text}")
            return False
            
        return True
    
    def parse_markdown_file(self, file_path: Path) -> Tuple[Dict, str]:
        """Parse WikiJS markdown file and extract frontmatter and content"""
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Extract YAML frontmatter
        frontmatter = {}
        if content.startswith('---'):
            parts = content.split('---', 2)
            if len(parts) >= 3:
                frontmatter_text = parts[1].strip()
                content = parts[2].strip()
                
                # Simple YAML parsing for basic fields
                for line in frontmatter_text.split('\n'):
                    if ':' in line:
                        key, value = line.split(':', 1)
                        frontmatter[key.strip()] = value.strip()
        
        return frontmatter, content
    
    def get_page_hierarchy(self) -> List[Tuple[Path, int]]:
        """Get all markdown files sorted by dependency order"""
        md_files = []
        
        for md_file in self.wiki_dir.rglob('*.md'):
            if md_file.name == 'README.md':
                continue
                
            relative_path = md_file.relative_to(self.wiki_dir)
            depth = len(relative_path.parts) - 1  # -1 because filename doesn't count as depth
            
            md_files.append((md_file, depth))
        
        # Sort by dependency order: parents must be created before children
        def sort_key(item):
            file_path, _ = item
            relative_path = file_path.relative_to(self.wiki_dir)
            
            # Create a sort key that ensures parents come before children
            # For hw-development/hw-documentation/tests.md:
            # - First sort by each path component level
            # - Then by the filename itself
            path_parts = relative_path.parts[:-1]  # Directory parts only
            filename = relative_path.stem
            
            # Create hierarchical sort key
            sort_parts = []
            for i, part in enumerate(path_parts):
                sort_parts.append((i, part))  # (level, directory_name)
            
            sort_parts.append((len(path_parts), filename))  # (level, filename)
            
            return sort_parts
        
        md_files.sort(key=sort_key)
        
        # Debug: print the order
        print("File processing order:")
        for i, (file_path, depth) in enumerate(md_files[:10]):  # Show first 10
            rel_path = file_path.relative_to(self.wiki_dir)
            print(f"  {i+1:2d}. {rel_path} (depth: {depth})")
        if len(md_files) > 10:
            print(f"  ... and {len(md_files) - 10} more files")
            
        return md_files
    
    def build_wiki_tree(self) -> Dict:
        """Build a tree structure representing WikiJS hierarchy"""
        tree = {}
        
        for md_file in self.wiki_dir.rglob('*.md'):
            if md_file.name == 'README.md':
                continue
                
            relative_path = md_file.relative_to(self.wiki_dir)
            path_parts = relative_path.parts
            
            # Navigate/create tree structure
            current_level = tree
            
            for i, part in enumerate(path_parts[:-1]):  # Exclude the .md file itself
                if part not in current_level:
                    current_level[part] = {
                        'children': {},
                        'md_file': None  # Directory might not have corresponding .md file
                    }
                current_level = current_level[part]['children']
            
            # Add the .md file
            filename = path_parts[-1]  # The .md file
            stem = relative_path.stem  # Filename without .md
            
            if stem not in current_level:
                current_level[stem] = {
                    'children': {},
                    'md_file': md_file
                }
            else:
                # Update existing entry with md_file
                current_level[stem]['md_file'] = md_file
        
        return tree
    
    def create_missing_parent(self, parent_path: Path) -> str:
        """Create a missing parent document to maintain hierarchy"""
        relative_path = parent_path.relative_to(self.wiki_dir)
        
        # Check if already exists
        if str(relative_path) in self.document_map:
            return self.document_map[str(relative_path)]
        
        # Create title from directory name
        title = parent_path.stem.replace('_', ' ').replace('-', ' ').title()
        content = f"# {title}\n\nThis page was automatically created to maintain hierarchy structure."
        
        print(f"  Creating missing parent: {relative_path}")
        document = self.create_document(title, content)
        self.document_map[str(relative_path)] = document['id']
        
        return document['id']
    
    def get_parent_from_tree(self, file_path: Path, tree: Dict) -> Optional[str]:
        """Find or create parent document ID using the tree structure"""
        relative_path = file_path.relative_to(self.wiki_dir)
        path_parts = relative_path.parts
        
        if len(path_parts) <= 1:  # Root level
            return None
        
        # Navigate up the tree to find or create the parent
        # For hw-development/hw-documentation/bringauto_pi-v4/tests/watchdog.md:
        # Look for tests.md, then bringauto_pi-v4.md, then hw-documentation.md, then hw-development.md
        
        current_path_parts = path_parts[:-1]  # Remove filename
        
        while current_path_parts:
            # Try to find parent.md in current directory
            parent_relative_path = Path(*current_path_parts).with_suffix('.md')
            
            parent_doc_id = self.document_map.get(str(parent_relative_path))
            if parent_doc_id:
                print(f"  Found parent '{parent_relative_path}' for '{relative_path}'")
                return parent_doc_id
            
            # If parent doesn't exist, create it
            parent_file_path = self.wiki_dir / parent_relative_path
            parent_doc_id = self.create_missing_parent(parent_file_path)
            
            # After creating this parent, it might also need a parent - recursively handle this
            parent_parent_id = self.get_parent_from_tree(parent_file_path, tree)
            if parent_parent_id:
                print(f"  Moving created parent '{parent_relative_path}' under its parent")
                self.move_document(parent_doc_id, parent_parent_id)
            
            return parent_doc_id
        
        return None
    
    def update_crosslinks(self, content: str) -> str:
        """Update WikiJS crosslinks to Outline format"""
        def replace_link(match):
            link_text = match.group(1)
            link_url = match.group(2)
            
            # Handle internal links (starting with /en/ or just /)
            if link_url.startswith('/en/'):
                page_path = link_url[4:]  # Remove '/en/'
            elif link_url.startswith('/'):
                page_path = link_url[1:]   # Remove '/'
            else:
                return match.group(0)  # Keep external links unchanged
            
            # Find corresponding document
            page_path_md = page_path + '.md'
            if page_path_md in self.document_map:
                doc_id = self.document_map[page_path_md]
                return f'[{link_text}](/doc/{doc_id})'
            
            return match.group(0)  # Keep original if not found
        
        # Replace markdown links
        content = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', replace_link, content)
        return content
    
    def update_image_links(self, content: str, base_path: Path) -> str:
        """Update image links in content, including WikiJS-style with sizing and HTML img tags"""
        md_rel = str(base_path.relative_to(self.wiki_dir))
        
        def replace_markdown_image(match):
            alt_text = match.group(1)
            img_path_with_params = match.group(2)
            
            # Split path from WikiJS sizing parameters (e.g., " =480x")
            img_path = img_path_with_params.split(' =')[0].strip()
            size_params = ""
            if ' =' in img_path_with_params:
                size_params = ' =' + img_path_with_params.split(' =', 1)[1]
            
            print(f"    Processing markdown image: {img_path}")
            if size_params:
                print(f"      Size params: {size_params}")
            
            return self._process_image_path(img_path, alt_text, base_path, md_rel, match.group(0))
        
        def replace_html_image(match):
            # Extract src, alt, and other attributes from HTML img tag
            img_tag = match.group(0)
            src_match = re.search(r'src=["\']([^"\']+)["\']', img_tag)
            alt_match = re.search(r'alt=["\']([^"\']*)["\']', img_tag)
            
            if not src_match:
                return img_tag  # Keep original if no src found
            
            img_path = src_match.group(1)
            alt_text = alt_match.group(1) if alt_match else ""
            
            print(f"    Processing HTML image: {img_path}")
            
            # Extract other attributes like height, width for potential preservation
            height_match = re.search(r'height=["\']([^"\']+)["\']', img_tag)
            width_match = re.search(r'width=["\']([^"\']+)["\']', img_tag)
            
            uploaded_url = self._process_image_path(img_path, alt_text, base_path, md_rel, img_tag, return_url_only=True)
            
            if uploaded_url != img_tag:  # If upload succeeded
                # Convert to markdown format, optionally preserving size info in alt text
                size_info = ""
                if height_match or width_match:
                    size_parts = []
                    if width_match:
                        size_parts.append(f"width={width_match.group(1)}")
                    if height_match:
                        size_parts.append(f"height={height_match.group(1)}")
                    size_info = f" ({', '.join(size_parts)})"
                
                return f'![{alt_text}{size_info}]({uploaded_url})'
            
            return img_tag  # Keep original if upload failed
        
        # Replace markdown image references
        content = re.sub(r'!\[([^]]*)\]\(([^)]+(?:\s*=\w*)?)\)', replace_markdown_image, content)
        
        # Replace HTML img tags
        content = re.sub(r'<img[^>]+>', replace_html_image, content)
        
        return content
    
    def _process_image_path(self, img_path: str, alt_text: str, base_path: Path, md_rel: str, fallback: str, return_url_only: bool = False) -> str:
        """Process an image path and upload if needed"""
        # Handle relative image paths
        if not img_path.startswith('http'):
            # Try to find the image file
            img_file = self._resolve_file_path(img_path, base_path)
            
            if img_file.exists():
                try:
                    uploaded_url = self.upload_attachment(img_file, md_rel)
                    if return_url_only:
                        return uploaded_url
                    return f'![{alt_text}]({uploaded_url})'
                except Exception as e:
                    self._log_event(md_rel, 'attachments', 'failed', f'image upload failed: {e}', {'file': str(img_file)})
                    print(f"Failed to upload image {img_file}: {e}")
            else:
                print(f"      Image file not found: {img_file}")
        
        return fallback  # Keep original if upload fails or external URL
    
    def convert_wikijs_blocks(self, content: str) -> str:
        """Convert WikiJS block extensions to Outline callouts"""
        # Map WikiJS block types to Outline callout types
        block_mapping = {
            'is-warning': 'warning',
            'is-danger': 'warning',
            'is-info': 'info',
            'is-success': 'tip',
            'is-primary': 'info',
            'is-secondary': 'info'
        }
        
        # Pattern to match WikiJS blocks: blockquote followed by {.class}
        # Matches: > content\n{.is-warning}
        def replace_block(match):
            blockquote_content = match.group(1).strip()
            block_class = match.group(2)
            
            # Remove the '>' prefix from each line and clean up
            lines = blockquote_content.split('\n')
            cleaned_lines = []
            for line in lines:
                line = line.strip()
                if line.startswith('>'):
                    line = line[1:].strip()
                if line:  # Only add non-empty lines
                    cleaned_lines.append(line)
            
            content_text = '\n'.join(cleaned_lines)
            
            # Get the appropriate Outline callout type
            outline_type = block_mapping.get(block_class, 'info')
            
            print(f"    Converting WikiJS block {{{block_class}}} to Outline :::{outline_type}")
            
            return f":::{outline_type}\n{content_text}\n:::"
        
        # Match blockquote followed by block class on next line
        # This handles both single-line and multi-line blockquotes
        pattern = r'((?:^>.*(?:\n|$))+)\s*\{\.(' + '|'.join(block_mapping.keys()) + r')\}'
        content = re.sub(pattern, replace_block, content, flags=re.MULTILINE)
        
        # Also handle inline block syntax: {.is-warning} at the end of a paragraph
        def replace_inline_block(match):
            paragraph_content = match.group(1).strip()
            block_class = match.group(2)
            outline_type = block_mapping.get(block_class, 'info')
            
            print(f"    Converting inline WikiJS block {{{block_class}}} to Outline :::{outline_type}")
            
            return f":::{outline_type}\n{paragraph_content}\n:::"
        
        # Match paragraph followed by block class
        inline_pattern = r'([^\n]+)\s*\{\.(' + '|'.join(block_mapping.keys()) + r')\}'
        content = re.sub(inline_pattern, replace_inline_block, content, flags=re.MULTILINE)
        
        return content
    
    def update_file_links(self, content: str, base_path: Path) -> str:
        """Update file links (non-images) in content"""
        md_rel = str(base_path.relative_to(self.wiki_dir))
        
        def replace_file_link(match):
            link_text = match.group(1)
            file_path = match.group(2)
            
            print(f"    Processing file link: {file_path}")
            
            # Handle relative file paths
            if not file_path.startswith('http'):
                # Try to find the file
                file_obj = self._resolve_file_path(file_path, base_path)
                
                if file_obj.exists():
                    try:
                        uploaded_url = self.upload_attachment(file_obj, md_rel)
                        return f'[{link_text}]({uploaded_url})'
                    except Exception as e:
                        self._log_event(md_rel, 'attachments', 'failed', f'file upload failed: {e}', {'file': str(file_obj)})
                        print(f"Failed to upload file {file_obj}: {e}")
                else:
                    print(f"      File not found: {file_obj}")
            
            return match.group(0)  # Keep original if upload fails
        
        # Replace file links - pattern for markdown links to files with extensions
        file_pattern = r'\[([^\]]*)\]\(([^)]*\.(?:xml|txt|csv|json|pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|7z|yaml|yml|drawio)(?:\s*=[^)]*)?)\)'
        content = re.sub(file_pattern, replace_file_link, content)
        return content
    
    def migrate(self):
        """Perform the complete migration"""
        print("Starting WikiJS to Outline migration...")
        
        # Create or get collection
        collections = self.get_collections()
        print(f"Found {len(collections)} collections")
        
        wiki_collection = None
        
        for collection in collections:
            if collection['name'] == 'WikiJS Import':
                wiki_collection = collection
                break
        
        if not wiki_collection:
            print("Creating new collection 'WikiJS Import'...")
            self.collection_id = self.create_collection('WikiJS Import', 'Migrated from WikiJS')
            print(f"Created collection with ID: {self.collection_id}")
        else:
            self.collection_id = wiki_collection['id']
            print(f"Using existing collection: {wiki_collection['name']} (ID: {self.collection_id})")
        
        # Test document creation permission
        print("Testing document creation permission...")
        try:
            test_doc = self.create_document("_TEST_DOC_DELETE_ME", "Test content")
            print(f"✓ Test document created successfully (ID: {test_doc['id']})")
            
            # Delete test document
            delete_response = self.session.post(f'{self.outline_url}/api/documents.delete', 
                                              json={'id': test_doc['id']})
            if delete_response.status_code == 200:
                print("✓ Test document deleted")
            else:
                print(f"Warning: Could not delete test document: {delete_response.status_code}")
                
        except Exception as e:
            print(f"✗ Document creation test failed: {e}")
            print("Checking collection permissions...")
            
            # Get collection info to check permissions
            try:
                coll_response = self.session.post(f'{self.outline_url}/api/collections.info', 
                                                json={'id': self.collection_id})
                coll_data = coll_response.json()['data']
                print(f"Collection permissions: {coll_data.get('permission', 'unknown')}")
            except Exception as perm_e:
                print(f"Could not check collection permissions: {perm_e}")
            
            raise Exception("Cannot create documents - check token permissions")
        
        # Get all markdown files in hierarchy order
        md_files = self.get_page_hierarchy()
        print(f"Found {len(md_files)} markdown files to migrate")
        
        # Process each file
        for file_path, _ in md_files:
            relative_path = file_path.relative_to(self.wiki_dir)
            rel_str = str(relative_path)
            print(f"Processing: {relative_path}")

            try:
                # Parse the markdown file
                frontmatter, content = self.parse_markdown_file(file_path)

                # Get title from frontmatter or filename
                title = frontmatter.get('title', file_path.stem.replace('_', ' ').title())

                # Convert WikiJS block extensions to Outline callouts
                content = self.convert_wikijs_blocks(content)

                # Update image links
                content = self.update_image_links(content, file_path)
                
                # Update file links (non-images)
                content = self.update_file_links(content, file_path)

                # Create document (all as root first)
                try:
                    document = self.create_document(title, content)
                    self._log_event(rel_str, 'document', 'success', 'Document created', {'id': document['id']})
                except requests.exceptions.RequestException as e:
                    self._log_event(rel_str, 'document', 'failed', f'documents.create failed: {getattr(e.response, "status_code", "?")}', {})
                    raise

                # Store mapping for crosslink updates
                self.document_map[rel_str] = document['id']
                self.url_map[f"/en/{relative_path.with_suffix('').as_posix()}"] = f"/doc/{document['id']}"

                print(f"  ✓ Created: {title} (ID: {document['id']})")

                # Rate limiting
                time.sleep(0.05)

            except Exception as e:
                self._log_event(rel_str, 'document', 'failed', f'processing failed: {e}', {})
                print(f"  ✗ Failed to process {relative_path}: {e}")
        
        # Second pass: Build WikiJS tree structure and organize hierarchy
        print("\nBuilding WikiJS tree structure...")
        wiki_tree = self.build_wiki_tree()
        
        print("Organizing document hierarchy...")
        
        for file_path, _ in md_files:
            relative_path = file_path.relative_to(self.wiki_dir)
            doc_id = self.document_map.get(str(relative_path))
            
            if not doc_id:
                continue
                
            # Get parent document ID using tree structure
            parent_id = self.get_parent_from_tree(file_path, wiki_tree)
            
            if parent_id and parent_id in self.document_map.values():
                print(f"  Moving '{relative_path}' under parent")
                success = self.move_document(doc_id, parent_id)
                if success:
                    self._log_event(str(relative_path), 'move', 'success', 'Document moved under parent', {'id': doc_id})
                    print(f"    ✓ Moved successfully")
                else:
                    self._log_event(str(relative_path), 'move', 'failed', 'Move failed', {'id': doc_id})
                    print(f"    ✗ Move failed")
                    
                time.sleep(0.05)  # Rate limiting
        
        # Third pass: Update all crosslinks
        print("\nUpdating crosslinks...")

        for file_path, _ in md_files:
            relative_path = file_path.relative_to(self.wiki_dir)
            rel_str = str(relative_path)
            doc_id = self.document_map.get(rel_str)
            
            if not doc_id:
                continue
                
            try:
                # Get current document content
                response = self.session.post(f'{self.outline_url}/api/documents.info', 
                                           json={'id': doc_id})
                response.raise_for_status()
                document = response.json()['data']
                
                # Update crosslinks
                updated_content = self.update_crosslinks(document['text'])
                
                if updated_content != document['text']:
                    # Update document
                    update_data = {
                        'id': doc_id,
                        'text': updated_content
                    }
                    response = self.session.post(f'{self.outline_url}/api/documents.update',
                                               json=update_data)
                    if response.status_code == 200:
                        self._log_event(str(relative_path), 'crosslinks', 'success', 'Crosslinks updated', {})
                    else:
                        self._log_event(str(relative_path), 'crosslinks', 'failed', f'documents.update failed: {response.status_code}', {})
                    response.raise_for_status()
                    print(f"  ✓ Updated crosslinks in: {relative_path}")
                
                time.sleep(0.05)
                
            except Exception as e:
                self._log_event(rel_str, 'crosslinks', 'failed', f'crosslinks update failed: {e}', {})
                print(f"  ✗ Failed to update crosslinks in {relative_path}: {e}")
        
        print(f"\nMigration completed! Processed {len(md_files)} documents.")
        print(f"Collection ID: {self.collection_id}")

        # Write migration logs
        self.write_log_files()
        print("Logs written: _outline_migration_log.md, _outline_migration_failures.csv")

def main():
    parser = argparse.ArgumentParser(description='Migrate WikiJS backup to Outline wiki')
    parser.add_argument('--outline-url', required=True, help='Outline instance URL')
    parser.add_argument('--token', required=True, help='Outline API token')
    parser.add_argument('--wiki-dir', default='wikijs-complete-export', required=True, help='WikiJS backup directory')
    
    args = parser.parse_args()
    
    converter = WikiJSToOutlineConverter(args.outline_url, args.token, args.wiki_dir)
    converter.migrate()

if __name__ == '__main__':
    main()