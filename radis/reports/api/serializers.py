from typing import Any

from rest_framework import serializers, validators
from rest_framework.exceptions import ValidationError

from ..models import Metadata, Modality, Report


class MetadataSerializer(serializers.ModelSerializer):
    class Meta:
        model = Metadata
        fields = ("key", "value")


class ModalitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Modality
        fields = ("code",)

    def run_validation(self, data: dict[str, Any]) -> Any:
        # We don't want to check if this modality already exists in the database
        # as later use get_or_create.
        for validator in self.fields["code"].validators:
            if isinstance(validator, validators.UniqueValidator):
                self.fields["code"].validators.remove(validator)
        return super().run_validation(data)


class ReportSerializer(serializers.ModelSerializer):
    metadata = MetadataSerializer(many=True)
    modalities = ModalitySerializer(many=True)

    class Meta:
        model = Report
        fields = "__all__"

    def create(self, validated_data: Any) -> Any:
        groups = validated_data.pop("groups")
        metadata = validated_data.pop("metadata")
        modalities = validated_data.pop("modalities")

        report = Report.objects.create(**validated_data)

        report.groups.set(groups)

        for metadata in metadata:
            Metadata.objects.create(report=report, **metadata)

        for modality in modalities:
            fetched_modality, _ = Modality.objects.get_or_create(**modality)
            report.modalities.add(fetched_modality)

        return report

    def update(self, report: Report, validated_data: Any) -> Any:
        groups = validated_data.pop("groups")
        metadata = validated_data.pop("metadata")
        modalities = validated_data.pop("modalities")

        report.groups.set(groups)

        report.metadata.all().delete()
        for metadata in metadata:
            Metadata.objects.create(report=report, **metadata)

        report.modalities.clear()
        for modality in modalities:
            fetched_modality, _ = Modality.objects.get_or_create(**modality)
            report.modalities.add(fetched_modality)

        return report

    def to_internal_value(self, data: Any) -> Any:
        if "metadata" in data:
            if not isinstance(data["metadata"], dict):
                raise ValidationError("Invalid metadata")
            data["metadata"] = [
                {"key": key, "value": value} for key, value in data["metadata"].items()
            ]

        if "modalities" in data:
            if not isinstance(data["modalities"], list):
                raise ValidationError("Invalid modalities")
            data["modalities"] = [{"code": code} for code in data["modalities"]]

        return super().to_internal_value(data)

    def to_representation(self, instance: Any) -> Any:
        ret = super().to_representation(instance)

        if "metadata" in ret:
            ret["metadata"] = {item["key"]: item["value"] for item in ret["metadata"]}

        if "modalities" in ret:
            ret["modalities"] = [item["code"] for item in ret["modalities"]]

        return ret
