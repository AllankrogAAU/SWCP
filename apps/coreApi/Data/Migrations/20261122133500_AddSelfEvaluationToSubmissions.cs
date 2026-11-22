using coreApi.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace coreApi.Data.Migrations;

[DbContext(typeof(CoreDbContext))]
[Migration("20261122133500_AddSelfEvaluationToSubmissions")]
public partial class AddSelfEvaluationToSubmissions : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(
            name: "DiagnosisConfidence",
            table: "Submissions",
            type: "character varying(32)",
            maxLength: 32,
            nullable: true);

        migrationBuilder.AddColumn<string>(
            name: "RootCauseCategory",
            table: "Submissions",
            type: "character varying(32)",
            maxLength: 32,
            nullable: true);
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropColumn(
            name: "DiagnosisConfidence",
            table: "Submissions");

        migrationBuilder.DropColumn(
            name: "RootCauseCategory",
            table: "Submissions");
    }
}
