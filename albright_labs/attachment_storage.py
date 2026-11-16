"""Where attachment bytes live - the only module that reads or writes them. Today: Postgres (AttachmentBlob, one row
per attachment holding the file and its thumbnail). Moving to object storage (e.g. DigitalOcean Spaces) means
rewriting these three functions to put / get / delete Attachment.key there; views, templates and
albright_labs.attachments don't change.

    save_attachment_bytes(attachment, data, thumb)     # after the Attachment row exists
    open_attachment(attachment, thumb=False)           # -> a file object, or None
    delete_attachment_bytes(attachment)                # Attachment post_delete (a no-op here: the blob cascades)
"""
import io

from .models import AttachmentBlob


def save_attachment_bytes(attachment, data, thumb=None):
    AttachmentBlob.objects.update_or_create(attachment=attachment, defaults={"data": data, "thumb_data": thumb})


def open_attachment(attachment, thumb=False):
    """The file (or its thumbnail) as a readable file object, or None if it isn't stored. Loads only the column
    asked for, so serving a thumbnail never reads the full file."""
    column = "thumb_data" if thumb else "data"
    row = AttachmentBlob.objects.filter(attachment_id=attachment.pk).values_list(column, flat=True).first()
    if row is None:
        return None
    return io.BytesIO(bytes(row))


def delete_attachment_bytes(attachment):
    """Nothing to do while the bytes are in Postgres: AttachmentBlob goes with its Attachment (on_delete=CASCADE)."""
    return None
