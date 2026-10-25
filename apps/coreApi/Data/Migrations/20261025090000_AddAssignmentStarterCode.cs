using coreApi.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace coreApi.Data.Migrations;

[DbContext(typeof(CoreDbContext))]
[Migration("20261025090000_AddAssignmentStarterCode")]
public partial class AddAssignmentStarterCode : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(
            name: "StarterCode",
            table: "Assignments",
            type: "text",
            nullable: true);

        migrationBuilder.Sql("""
            UPDATE "Assignments"
            SET "StarterCode" = E'int add(int left, int right)\n{\n    return 0;\n}'
            WHERE "Id" = 'a5555555-5555-4555-8555-555555555555'
              AND "HarnessSource" IS NOT NULL;
            """);
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropColumn(
            name: "StarterCode",
            table: "Assignments");
    }
}
