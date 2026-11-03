import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, type Query } from "../../api/client";
import type {
  AssetKind,
  AssetRef,
  Chapter,
  Course,
  CourseDetail,
  CourseInput,
  CoursePage,
  CourseSummary,
  Enrollment,
  EnrollmentDetail,
  EnrollmentPage,
  Lecture,
  Playback,
  Playlist,
  PublishCheck,
  Resource,
  StorageStatus,
  StudentCourse,
  TaxonomyKind,
  TaxonomyTerm,
  UploadTicket,
  Variant,
} from "./types";

/** Base path of the course admin API (the AdhyaapakAI backend serves the same sub-paths under
 * /api/admin/courses, so moving there changes only this constant). */
export const COURSES_API = "/courses";

// Every key starts with "courses", so one invalidation refreshes the module.
export const courseKeys = {
  all: ["courses"] as const,
  summary: ["courses", "summary"] as const,
  list: (q: Query) => ["courses", "list", q] as const,
  detail: (id: number) => ["courses", "detail", id] as const,
  check: (id: number) => ["courses", "check", id] as const,
  preview: (id: number) => ["courses", "preview", id] as const,
  taxonomy: ["courses", "taxonomy"] as const,
  storage: ["courses", "storage"] as const,
  enrollments: (q: Query) => ["courses", "enrollments", q] as const,
  enrollment: (id: number) => ["courses", "enrollment", id] as const,
};

export const useCourseSummary = () =>
  useQuery({ queryKey: courseKeys.summary, queryFn: () => apiFetch<CourseSummary>(`${COURSES_API}/summary`) });

export const useCourses = (q: Query) =>
  useQuery({ queryKey: courseKeys.list(q), queryFn: () => apiFetch<CoursePage>(COURSES_API, { query: q }), placeholderData: keepPreviousData });

export const useCourse = (id: number) =>
  useQuery({ queryKey: courseKeys.detail(id), queryFn: () => apiFetch<CourseDetail>(`${COURSES_API}/${id}`) });

export const usePublishCheck = (id: number) =>
  useQuery({ queryKey: courseKeys.check(id), queryFn: () => apiFetch<PublishCheck>(`${COURSES_API}/${id}/publish-check`) });

export const useCoursePreview = (id: number) =>
  useQuery({ queryKey: courseKeys.preview(id), queryFn: () => apiFetch<StudentCourse>(`${COURSES_API}/${id}/preview`) });

export const useTaxonomy = () =>
  useQuery({ queryKey: courseKeys.taxonomy, queryFn: () => apiFetch<TaxonomyTerm[]>(`${COURSES_API}/taxonomy`), staleTime: 60_000 });

export const useStorageStatus = () =>
  useQuery({ queryKey: courseKeys.storage, queryFn: () => apiFetch<StorageStatus>(`${COURSES_API}/storage`), staleTime: 300_000 });

export const useEnrollments = (q: Query) =>
  useQuery({ queryKey: courseKeys.enrollments(q), queryFn: () => apiFetch<EnrollmentPage>(`${COURSES_API}/enrollments`, { query: q }), placeholderData: keepPreviousData });

export const useEnrollment = (id: number) =>
  useQuery({ queryKey: courseKeys.enrollment(id), queryFn: () => apiFetch<EnrollmentDetail>(`${COURSES_API}/enrollments/${id}`) });

/** Lecture playback in preview mode (owner/viewer; server-signed URLs). */
export const previewLecture = (courseId: number, lectureId: number, language?: string) =>
  apiFetch<Playback>(`${COURSES_API}/${courseId}/preview/lectures/${lectureId}`, { query: { language } });

export const previewResource = (courseId: number, resourceId: number) =>
  apiFetch<{ url: string; expires_in: number }>(`${COURSES_API}/${courseId}/preview/resources/${resourceId}`);

function useCourseMutation<TVars, TResult>(fn: (vars: TVars) => Promise<TResult>) {
  const client = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => client.invalidateQueries({ queryKey: courseKeys.all }) });
}

type WithVersion = CourseInput & { version: number };

export const useCreateCourse = () => useCourseMutation((body: CourseInput) => apiFetch<CourseDetail>(COURSES_API, { method: "POST", body }));
export const useUpdateCourse = () =>
  useCourseMutation(({ id, body }: { id: number; body: WithVersion }) => apiFetch<CourseDetail>(`${COURSES_API}/${id}`, { method: "PUT", body }));
export const useDeleteCourse = () => useCourseMutation((id: number) => apiFetch<void>(`${COURSES_API}/${id}`, { method: "DELETE" }));
export const useCourseAction = () =>
  useCourseMutation(({ id, action }: { id: number; action: "publish" | "unpublish" | "archive" | "unarchive" }) =>
    apiFetch<CourseDetail>(`${COURSES_API}/${id}/${action}`, { method: "POST" }));

export const useSaveTerm = () =>
  useCourseMutation(({ id, body }: { id?: number; body: { kind?: TaxonomyKind; name?: string; is_active?: boolean } }) =>
    id ? apiFetch<TaxonomyTerm>(`${COURSES_API}/taxonomy/${id}`, { method: "PUT", body }) : apiFetch<TaxonomyTerm>(`${COURSES_API}/taxonomy`, { method: "POST", body }));
export const useDeleteTerm = () => useCourseMutation((id: number) => apiFetch<void>(`${COURSES_API}/taxonomy/${id}`, { method: "DELETE" }));

type Named = { title: string; description: string | null; thumbnail_asset_id: number | null };
export type LectureBody = Named & { duration_seconds: number | null; is_free_preview: boolean; status: "draft" | "published"; default_language: string | null };
export type VariantBody = {
  language: string;
  video_asset_id: number | null;
  video_url: string | null;
  audio_asset_id: number | null;
  subtitle_asset_id: number | null;
  thumbnail_asset_id: number | null;
  description: string | null;
};
export type ResourceBody = { title: string; asset_id: number; is_downloadable: boolean };

/** Create (parentPath given) or update (selfPath given) any content node. */
export const useSaveNode = <T,>() =>
  useCourseMutation(({ create, path, body }: { create: boolean; path: string; body: unknown }) =>
    apiFetch<T>(path, { method: create ? "POST" : "PUT", body }));
export const useDeleteNode = () => useCourseMutation((path: string) => apiFetch<void>(path, { method: "DELETE" }));
export const useReorder = () =>
  useCourseMutation(({ path, ids }: { path: string; ids: number[] }) => apiFetch<void>(path, { method: "PUT", body: { ids } }));

export const useCancelEnrollment = () =>
  useCourseMutation(({ id, reason }: { id: number; reason: string }) =>
    apiFetch<EnrollmentDetail>(`${COURSES_API}/enrollments/${id}/cancel`, { method: "POST", body: { reason } }));

export const requestUpload = (body: { kind: AssetKind; filename: string; content_type: string; size_bytes: number; course_id?: number }) =>
  apiFetch<UploadTicket>(`${COURSES_API}/assets/uploads`, { method: "POST", body });
export const completeUpload = (id: number) => apiFetch<AssetRef>(`${COURSES_API}/assets/${id}/complete`, { method: "POST" });

export type { Chapter, Course, Enrollment, Lecture, Playlist, Resource, Variant };
