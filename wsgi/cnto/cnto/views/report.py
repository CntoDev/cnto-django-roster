import csv
import calendar
import traceback

from django.utils.timezone import datetime, timedelta
from django.http.response import JsonResponse
from django.utils import timezone
from django.http import HttpResponse
from django.shortcuts import redirect, render

from django.db.models import Max, Min

from cnto.templatetags.cnto_tags import has_permission
from cnto_warnings.models import MemberWarning
from ..models import MemberGroup, Event, Member, Attendance, Absence, AbsenceType


def get_summary_data(request):
    if not request.user.is_authenticated():
        return redirect("login")
    elif not has_permission(request.user, "cnto_view_reports"):
        return redirect("manage")

    first_event_dt = Event.objects.all().aggregate(Min('start_dt'))["start_dt__min"]
    last_event_dt = Event.objects.all().aggregate(Max('start_dt'))["start_dt__max"]

    first_event_sunday = first_event_dt

    while first_event_sunday.weekday() != 6:
        first_event_sunday = first_event_sunday - timedelta(days=1)

    week_start_dt = first_event_sunday
    week_end_dt = first_event_sunday + timedelta(days=7)

    event_data = []

    while week_start_dt < last_event_dt:
        week_events = Event.objects.filter(start_dt__gte=week_start_dt, end_dt__lt=week_end_dt)

        week_event_count = len(week_events)
        total_attendances = 0
        max_attendance = 0
        for event in week_events:
            event_attendances = Attendance.objects.filter(event=event, member__deleted=False)
            event_attendance_count = len(event_attendances)
            total_attendances += event_attendance_count
            max_attendance = max(max_attendance, event_attendance_count)

        event_data.append({
            "week_start_dt": week_start_dt.strftime("%Y-%m-%d"),
            "week_end_dt": week_end_dt.strftime("%Y-%m-%d"),
            "week_max": max_attendance,
            "week_avg": float(total_attendances) / week_event_count if week_event_count > 0 else 0
        })

        week_start_dt = week_end_dt
        week_end_dt += timedelta(days=7)

    return JsonResponse({
        "event-data": event_data,
    })


def get_warnings_for_date_range(start_dt, end_dt, include_recruits=True):
    """

    :param start_dt:
    :param end_dt:
    :param include_recruits:
    :return:
    """
    warnings = MemberWarning.objects.all()

    return sorted(warnings, key=lambda warning: warning.member.name)


def report_main(request):
    """Browse reports
    """

    if not request.user.is_authenticated():
        return redirect("login")
    elif not has_permission(request.user, "cnto_view_reports"):
        return redirect("manage")

    context = {
        "warning_count": MemberWarning.objects.filter(acknowledged=False).count()
    }

    return render(request, 'cnto/report/report-main.html', context)


def get_report_context_for_date_range(start_dt, end_dt):
    try:
        context = {}

        events = Event.all_for_time_period(start_dt, end_dt).order_by("start_dt")
        context["event_count"] = events.count()

        events_dict = {
            "start_dates": [event.start_dt.strftime("%Y-%m-%d") for event in events],
            "css_classes": [event.event_type.css_class_name for event in events]
        }
        context["events"] = events_dict

        groups = MemberGroup.objects.all().order_by("name")
        all_members = Member.active_members()

        attendance_dict = {}
        group_members = {}
        for group in groups:
            attendance_dict[group.name] = {}
            members = all_members.filter(member_group=group).order_by("name")
            for member in members:
                # print "Reporting member %s..." % (member, )
                period_attendance_adequate, reason = Attendance.was_adequate_for_period(member, events, start_dt,
                                                                                        end_dt, adequate_if_absent=True)
                attendance_dict[group.name][member.name] = {
                    "attendance_adequate": period_attendance_adequate,
                    "attendances": []
                }
                if group.name not in group_members:
                    group_members[group.name] = []

                group_members[group.name].append(member.name)

                for event in events:
                    try:
                        if member.is_recruit() and not member.mods_assessed:
                            absence_type = "-"
                        else:
                            if member.join_date > event.start_dt.date():
                                absence_type = "-"
                            else:
                                Absence.get_absence_for_event(event, member)
                                absence_type = "LOA"
                    except Absence.DoesNotExist:
                        absence_type = None

                    try:
                        attendance = Attendance.objects.get(member=member, event=event)
                        was_adequate = attendance.was_adequate()
                        if was_adequate:
                            presence_marker = "X"
                        else:
                            presence_marker = "?"

                    except Attendance.DoesNotExist:
                        presence_marker = " "

                    if absence_type is not None:
                        presence_marker = absence_type + " " + presence_marker

                    attendance_dict[group.name][member.name]["attendances"].append(presence_marker)

        context["attendances"] = attendance_dict

        context["group_names"] = sorted(group_members.keys())
        context["group_members"] = group_members
        context["start_dt"] = start_dt.strftime("%Y-%m-%d")
        context["end_dt"] = end_dt.strftime("%Y-%m-%d")
    except Exception:
        print(str(traceback.format_exc()))
        raise

    return context


def get_report_body_for_month(request, month_string):
    """Get reports
    """
    if not request.user.is_authenticated():
        return redirect("login")
    elif not has_permission(request.user, "cnto_view_reports"):
        return redirect("manage")

    month_dt = datetime.strptime(month_string, "%Y-%m")

    context = get_report_context_for_date_range(
        timezone.make_aware(datetime(month_dt.year, month_dt.month, 1, 0, 0), timezone.get_default_timezone()),
        timezone.make_aware(datetime(month_dt.year, month_dt.month,
                                     calendar.monthrange(month_dt.year, month_dt.month)[1], 23, 59),
                            timezone.get_default_timezone()))

    return JsonResponse(context)


def download_report_for_month(request, dt_string, group_pk=None):
    if not request.user.is_authenticated():
        return redirect("login")
    elif not has_permission(request.user, "cnto_view_reports"):
        return redirect("manage")

    group = MemberGroup.objects.get(pk=group_pk)
    dt = datetime.strptime(dt_string, "%Y-%m-%d")

    events = Event.objects.filter(start_dt__year=dt.year, start_dt__month=dt.month).order_by("start_dt")

    members = Member.objects.filter(member_group=group).order_by("name")

    filename = "%s-%s.csv" % (dt.strftime("%Y-%m"), group.name.lower())

    # Create the HttpResponse object with the appropriate CSV header.
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="%s"' % (filename,)
    writer = csv.writer(response)

    header_columns = ["Member"]
    for event in events:
        header_columns.append(event.start_dt.strftime("%Y-%m-%d"))
    writer.writerow(header_columns)

    for member in members:
        member_columns = [member.name]

        for event in events:
            was_adequate = False
            try:
                attendance = Attendance.objects.get(member=member, event=event)
                was_adequate = attendance.was_adequate()
            except Attendance.DoesNotExist:
                pass

            if was_adequate:
                member_columns.append("X")
            else:
                member_columns.append(" ")

        writer.writerow(member_columns)

    writer.writerow([])
    writer.writerow(["X = attended"])

    return response
