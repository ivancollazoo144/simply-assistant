# Privacy Policy

**Application:** Simply Assistant
**Operator:** Simplicity Learning Center
**Effective date:** May 29, 2026

## 1. Scope

This Privacy Policy describes how Simply Assistant ("the Application") handles data. The Application is a single-user, internally hosted administrative tool used exclusively by the owner/administrator of Simplicity Learning Center ("the School"). It is not offered as a service to any third party.

## 2. Data Collected

The Application accesses and processes the following categories of data, with the user's explicit authorization:

- **QuickBooks Online data:** chart of accounts, transactions, customer records, invoices, and financial reports, accessed via the Intuit QuickBooks Online API under the user's own OAuth grant.
- **Family and student records:** maintained in a local SQLite database.
- **Calendar events and reminders:** created in the user's own Google Calendar and Google Tasks via Google APIs under the user's own OAuth grant.
- **Email drafts:** created in the user's own Gmail account via the Gmail API under the user's own OAuth grant.
- **Conversation history:** stored locally for context continuity.

## 3. Where Data Is Stored

All data is stored locally on hardware owned and controlled by the School. The Application does not transmit data to any remote server controlled by the developers, nor does it sync data to any cloud storage other than the third-party services the user has explicitly authorized (QuickBooks Online, Google Workspace, Anthropic API for AI inference).

## 4. Third-Party Services

The Application uses the following third-party services strictly to perform its core functions:

- **Anthropic API (Claude):** for natural-language interaction. User prompts and tool outputs are transmitted to Anthropic per their API terms; Anthropic does not train on API data.
- **Intuit QuickBooks Online API:** for reading and (with approval) writing bookkeeping data.
- **Google APIs (Calendar, Tasks, Gmail):** for scheduling and email-draft features.
- **Telegram Bot API:** for mobile chat access by the authorized user only.

No data is shared with any third party beyond what is strictly necessary to invoke these services on the user's behalf.

## 5. No Sale or Sharing

The Application does not sell, rent, lease, or otherwise share user data with any third party for marketing or commercial purposes.

## 6. Data Retention and Deletion

All data persists locally until the user deletes it. The user may delete the local database, OAuth tokens, and conversation history at any time. Revoking OAuth access from the user's QuickBooks, Google, or Telegram accounts immediately disconnects the Application's access to those services.

## 7. Security

Data is protected by the security controls of the underlying operating system, including FileVault disk encryption. OAuth tokens are stored in local files with standard filesystem permissions. The user is responsible for physical and account security of the host device.

## 8. Children's Privacy

The Application is not directed at children and does not knowingly collect data from children. It is used by a single adult administrator to manage records that may include information about the School's enrolled students; such records are handled per the School's own student-privacy obligations.

## 9. Changes to This Policy

This Privacy Policy may be updated at any time without notice. The "Effective date" at the top reflects the most recent revision.

## 10. Contact

For questions regarding this Privacy Policy, contact the School's administrator directly.
