// Course management API shapes (backend: app/schemas/course.py).
import type { Decimal, IsoDateTime } from "../../api/types";

export type CourseStatus = "draft" | "published" | "archived";
export type DisplayStatus = "draft" | "scheduled" | "published" | "archived";
export type ValidityUnit = "days" | "months" | "years";
export type TaxonomyKind = "class" | "stream" | "subject";
export type AssetKind = "image" | "video" | "audio" | "subtitle" | "document";
export type LectureStatus = "draft" | "published";
export type EnrollmentStatus = "pending" | "active" | "expired" | "cancelled" | "refunded";

export interface AssetRef {
  id: number;
  kind: AssetKind;
  status: "pending" | "uploaded";
  filename: string;
  content_type: string;
  size_bytes: number;
  url: string | null;
}

export interface TaxonomyTerm {
  id: number;
  kind: TaxonomyKind;
  name: string;
  sort_order: number;
  is_active: boolean;
  course_count: number;
}

export interface CourseInput {
  title: string;
  description: string | null;
  goals: string | null;
  thumbnail_asset_id: number | null;
  class_id: number | null;
  stream_id: number | null;
  subject_id: number | null;
  is_free: boolean;
  currency: string;
  price: Decimal | null;
  original_price: Decimal | null;
  discounted_price: Decimal | null;
  launch_at: IsoDateTime | null;
  validity_value: number;
  validity_unit: ValidityUnit;
}

export interface Course extends Omit<CourseInput, "thumbnail_asset_id"> {
  id: number;
  thumbnail: AssetRef | null;
  class_name: string | null;
  stream_name: string | null;
  subject_name: string | null;
  effective_price: Decimal;
  status: CourseStatus;
  display_status: DisplayStatus;
  validity_label: string;
  published_at: IsoDateTime | null;
  archived_at: IsoDateTime | null;
  playlist_count: number;
  lecture_count: number;
  enrollment_count: number;
  active_enrollment_count: number;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  version: number;
  can_delete: boolean;
}

export interface CoursePage {
  items: Course[];
  total: number;
  limit: number;
  offset: number;
}

export interface Variant {
  id: number;
  language: string;
  language_name: string;
  video: AssetRef | null;
  video_url: string | null;
  audio: AssetRef | null;
  subtitle: AssetRef | null;
  thumbnail: AssetRef | null;
  description: string | null;
}

export interface Resource {
  id: number;
  title: string;
  asset: AssetRef;
  is_downloadable: boolean;
  position: number;
}

export interface Lecture {
  id: number;
  title: string;
  description: string | null;
  thumbnail: AssetRef | null;
  duration_seconds: number | null;
  is_free_preview: boolean;
  status: LectureStatus;
  default_language: string | null;
  position: number;
  variants: Variant[];
  resources: Resource[];
}

export interface Chapter {
  id: number;
  title: string;
  description: string | null;
  thumbnail: AssetRef | null;
  position: number;
  lectures: Lecture[];
}

export interface Playlist {
  id: number;
  title: string;
  description: string | null;
  thumbnail: AssetRef | null;
  position: number;
  chapters: Chapter[];
}

export interface CourseDetail extends Course {
  playlists: Playlist[];
}

export interface PublishCheck {
  ready: boolean;
  problems: { code: string; message: string; blocking: boolean }[];
}

export interface StorageStatus {
  configured: boolean;
  max_video_mb: number;
  max_file_mb: number;
  allowed_types: Record<AssetKind, string[]>;
}

export interface UploadTicket {
  asset: AssetRef;
  upload_url: string;
  fields: Record<string, string>;
  expires_in: number;
}

export interface Enrollment {
  id: number;
  course_id: number;
  course_title: string;
  student_id: string;
  student_name: string | null;
  source: "payment" | "free";
  status: EnrollmentStatus;
  payment_provider: string | null;
  payment_id: string | null;
  order_id: string | null;
  currency: string;
  list_price: Decimal;
  amount_paid: Decimal;
  voucher_code: string | null;
  purchased_at: IsoDateTime | null;
  access_start_at: IsoDateTime | null;
  validity_value: number;
  validity_unit: ValidityUnit;
  validity_label: string;
  access_expires_at: IsoDateTime | null;
  status_reason: string | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface EnrollmentDetail extends Enrollment {
  events: { id: number; event: string; actor: string; details: Record<string, unknown> | null; occurred_at: IsoDateTime }[];
}

export interface EnrollmentPage {
  items: Enrollment[];
  total: number;
  limit: number;
  offset: number;
}

export interface CourseSummary {
  total_courses: number;
  published: number;
  scheduled: number;
  draft: number;
  archived: number;
  total_enrollments: number;
  active_enrollments: number;
  expired_enrollments: number;
  pending_enrollments: number;
  revenue: { currency: string; amount: Decimal }[];
}

export interface StudentLecture {
  id: number;
  title: string;
  duration_seconds: number | null;
  is_free_preview: boolean;
  languages: string[];
  resource_count: number;
}

export interface StudentCourse {
  id: number;
  title: string;
  description: string | null;
  goals: string | null;
  thumbnail_url: string | null;
  is_free: boolean;
  currency: string;
  price: Decimal | null;
  original_price: Decimal | null;
  effective_price: Decimal;
  validity_label: string;
  availability: { purchasable: boolean; reason: string | null; launch_at: IsoDateTime | null };
  playlists: { id: number; title: string; description: string | null; chapters: { id: number; title: string; lectures: StudentLecture[] }[] }[];
}

export interface Playback {
  lecture_id: number;
  title: string;
  language: string;
  language_name: string;
  available_languages: { code: string; name: string }[];
  video_url: string;
  audio_url: string | null;
  subtitle_url: string | null;
  url_expires_in: number | null;
  free_preview: boolean;
  resources: { id: number; title: string; is_downloadable: boolean }[];
}
