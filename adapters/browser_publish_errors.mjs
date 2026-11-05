// Public error symbols only. Never return Chromium logs, argv or credentials.
const publicCodes=new Set([
 'ACCOUNT_UI_NOT_AVAILABLE','ARTIFACT_CHANGED_BEFORE_SEND','ARTIFACT_INVALID','AUTH_EXPIRED',
 'DISPATCH_COMMIT_REQUIRED','DRAFT_CAPTION_CHANGED','DUPLICATE_RESULT_AMBIGUOUS',
 'EXISTING_DRAFT_IMAGES_REQUIRE_REVIEW','EXISTING_SOURCE_DIFFERENT_CAPTION',
 'IMAGE_UPLOAD_BLOCKED_SUBMISSION','IMAGE_UPLOAD_NOT_COMPLETED','PUBLISH_BUTTON_DISABLED','PUBLISH_RESULT_UNKNOWN',
 'RESULT_AUTHOR_UNVERIFIED','RESULT_MEDIA_NOT_LOADED','RESULT_SOURCE_UNVERIFIED','RESULT_TIME_LINK_UNVERIFIED',
 'RESULT_URL_UNVERIFIED','SPEC_INVALID','SPEC_TOO_LARGE','TARGET_ACCOUNT_MISMATCH',
 'TERMS_OWNER_CONFIRMATION_REQUIRED','TERMS_TEXT_NOT_LOADED','UNRELATED_DRAFT_REQUIRES_REVIEW',
 'MACHINE_DISPATCH_WINDOW_INVALID','OWNER_TWO_HOUR_WINDOW_EXPIRED',
 'WEB_BROWSER_PROFILE_LOCKED','WEB_UI_TIMEOUT','WEB_RUNTIME_ERROR',
]);

export function classifyPublisherError(error,stage){
 const message=typeof error?.message==='string'?error.message:'';
 if(publicCodes.has(message))return message;
 if(error?.name==='TimeoutError')return 'WEB_UI_TIMEOUT';
 if(stage==='launch'&&(
  /\bprofile(?: directory)? (?:appears to be|is(?: already)?) in use\b/i.test(message)||
  /\buser data directory is already in use\b/i.test(message)||
  /\bSingletonLock\b\s*:\s*(?:File exists|already exists)\b/i.test(message)||
  /\bSingletonLock\b\s+(?:is\s+)?(?:already\s+)?in use\b/i.test(message)
 ))return 'WEB_BROWSER_PROFILE_LOCKED';
 return 'WEB_RUNTIME_ERROR';
}
