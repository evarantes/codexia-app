import os
import boto3
from botocore.exceptions import NoCredentialsError

class StorageService:
    def __init__(self):
        self.endpoint_url = os.getenv('S3_ENDPOINT')
        self.access_key = os.getenv('S3_ACCESS_KEY')
        self.secret_key = os.getenv('S3_SECRET_KEY')
        self.bucket_name = os.getenv('S3_BUCKET', 'codexia-assets')
        
        self.s3 = None
        if self.endpoint_url and self.access_key and self.secret_key:
            try:
                self.s3 = boto3.client(
                    's3',
                    endpoint_url=self.endpoint_url,
                    aws_access_key_id=self.access_key,
                    aws_secret_access_key=self.secret_key
                )
                # Ensure bucket exists
                try:
                    self.s3.head_bucket(Bucket=self.bucket_name)
                except:
                    self.s3.create_bucket(Bucket=self.bucket_name)
            except Exception as e:
                print(f"Error initializing S3 client: {e}")

    def upload_file(self, file_path, object_name=None):
        """Upload a file to S3 bucket"""
        if not self.s3:
            return file_path  # Return local path if S3 not configured

        if object_name is None:
            object_name = os.path.basename(file_path)

        try:
            self.s3.upload_file(file_path, self.bucket_name, object_name)
            # Return S3 URL or Key
            # For MinIO, it might be http://minio:9000/bucket/key
            # We will return the key for now, or a presigned URL if needed.
            # Let's return the s3:// path or just the key to be consistent.
            # Actually, returning the public URL (if available) or the internal endpoint URL is better.
            return f"{self.endpoint_url}/{self.bucket_name}/{object_name}"
        except FileNotFoundError:
            print("The file was not found")
            return None
        except NoCredentialsError:
            print("Credentials not available")
            return None
        except Exception as e:
            print(f"Error uploading to S3: {e}")
            return None

    def download_file(self, object_name, file_path):
        """Download a file from S3 bucket"""
        if not self.s3:
            return False

        try:
            # If object_name is a full URL, extract the key
            if object_name.startswith(self.endpoint_url):
                object_name = object_name.replace(f"{self.endpoint_url}/{self.bucket_name}/", "")
            
            self.s3.download_file(self.bucket_name, object_name, file_path)
            return True
        except Exception as e:
            print(f"Error downloading from S3: {e}")
            return False
